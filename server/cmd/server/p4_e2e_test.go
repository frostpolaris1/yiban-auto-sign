package main

// P4-F e2e：计划、容量、手动签到、窗口关闭、人工覆盖与自选时间片。
//
// # 功能
// 用真实 PostgreSQL 与内存会话直接向路由发请求。
// 钉住四条验收：两校互不串窗、容量缺口如实上报、
// 手动签到四类结论、覆盖重排 pending。
//
// # 归属
// cmd/server 的 e2e。复用 w2cEnv 的 schema 隔离与 HTTP 客户端。
//
// # 口径
// 窗口发现由 e2eDiscoverer 预置，不发平台请求。
// 缺 DSN 时打可见横幅再跳过。

import (
	"fmt"
	"strings"
	"testing"
	"time"

	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

func p4Location(t *testing.T) *time.Location {
	t.Helper()
	// 中国标准时间无夏令时，固定偏移与 IANA 库等价。
	return time.FixedZone("CST", 8*3600)
}

func (e *w2cEnv) activateAccount(id string) {
	e.t.Helper()
	if _, err := e.db.Exec(e.ctx,
		"UPDATE accounts SET status = $2 WHERE id = $1::uuid",
		id, string(coreaccount.StatusActive)); err != nil {
		e.t.Fatalf("activate account: %v", err)
	}
}

func (e *w2cEnv) schoolTask(schoolID, name string) string {
	e.t.Helper()
	id, err := e.sched.CreateSchoolTask(e.ctx, schoolID, name)
	if err != nil {
		e.t.Fatalf("create school task: %v", err)
	}
	return id
}

// windowAt 返回业务日当天的起止时刻。
func windowAt(day string, loc *time.Location, startHM, endHM [2]int) (time.Time, time.Time) {
	d, _ := time.ParseInLocation("2006-01-02", day, loc)
	start := time.Date(d.Year(), d.Month(), d.Day(), startHM[0], startHM[1], 0, 0, loc)
	end := time.Date(d.Year(), d.Month(), d.Day(), endHM[0], endHM[1], 0, 0, loc)
	return start, end
}

// 两所学校各自的任务使用不同窗口排期，落点互不越界。
func TestP4TwoSchoolsIndependent(t *testing.T) {
	e := w2cNewEnv(t)
	loc := p4Location(t)
	day := clock.BusinessDay(clock.System{}.Now(), loc)
	master := e.user("p4-two-master@example.com", coreauth.RoleMaster)
	issued := e.login(master)

	schoolA := e.school("p4-two-a")
	schoolB := e.school("p4-two-b")
	taskA := e.schoolTask(schoolA.ID, "晨签")
	taskB := e.schoolTask(schoolB.ID, "晚签")
	acctA := e.account(schoolA.ID, master.ID, "13800009001")
	acctB := e.account(schoolB.ID, master.ID, "13800009002")
	e.activateAccount(acctA)
	e.activateAccount(acctB)

	startA, endA := windowAt(day, loc, [2]int{6, 30}, [2]int{7, 50})
	startB, endB := windowAt(day, loc, [2]int{20, 0}, [2]int{21, 0})
	e.windows[taskA] = contract.Window{Start: startA, End: endA, Source: contract.WindowPlatform}
	e.windows[taskB] = contract.Window{Start: startB, End: endB, Source: contract.WindowPlatform}

	planA := e.plan(t, issued, day, schoolA.ID, taskA)
	planB := e.plan(t, issued, day, schoolB.ID, taskB)

	if !planA.WindowStart.Equal(startA) || !planA.WindowEnd.Equal(endA) {
		t.Fatalf("甲校窗口 = [%v,%v], want [%v,%v]", planA.WindowStart, planA.WindowEnd, startA, endA)
	}
	if !planB.WindowStart.Equal(startB) || !planB.WindowEnd.Equal(endB) {
		t.Fatalf("乙校窗口 = [%v,%v], want [%v,%v]", planB.WindowStart, planB.WindowEnd, startB, endB)
	}
	if !planB.WindowStart.After(planA.WindowEnd) {
		t.Fatalf("两窗重叠: A=[%v,%v] B=[%v,%v]", planA.WindowStart, planA.WindowEnd, planB.WindowStart, planB.WindowEnd)
	}
	for _, r := range planA.Rows {
		if r.RunAt.Before(planA.WindowStart) || !r.RunAt.Before(planA.WindowEnd) {
			t.Fatalf("甲校行越窗: %v", r.RunAt)
		}
	}
	for _, r := range planB.Rows {
		if r.RunAt.Before(planB.WindowStart) || !r.RunAt.Before(planB.WindowEnd) {
			t.Fatalf("乙校行越窗: %v", r.RunAt)
		}
	}
	if n := e.scalarInt("SELECT count(*) FROM sign_tasks WHERE school_task_id = $1::uuid", taskA); n != 1 {
		t.Fatalf("甲校落库行数 = %d, want 1", n)
	}
	if n := e.scalarInt("SELECT count(*) FROM sign_tasks WHERE school_task_id = $1::uuid", taskB); n != 1 {
		t.Fatalf("乙校落库行数 = %d, want 1", n)
	}
}

// 容量缺口如实上报；超容量的行仍在窗内。
func TestP4CapacityShortfall(t *testing.T) {
	e := w2cNewEnv(t)
	loc := p4Location(t)
	day := clock.BusinessDay(clock.System{}.Now(), loc)
	master := e.user("p4-cap-master@example.com", coreauth.RoleMaster)
	issued := e.login(master)
	school := e.school("p4-cap")
	task := e.schoolTask(school.ID, "晨签")

	start, end := windowAt(day, loc, [2]int{6, 30}, [2]int{7, 50})
	e.windows[task] = contract.Window{Start: start, End: end, Source: contract.WindowPlatform}

	// 81 个活跃账号；配置为 1 通道、每次 60s、窗口 80 分钟、util 0.8
	// → 容量 floor(4800/60×0.8)=64，缺口 17。
	for i := 0; i < 81; i++ {
		id := e.account(school.ID, master.ID, fmt.Sprintf("138%08d", 10000+i))
		e.activateAccount(id)
	}

	rec := e.do("GET", "/api/capacity?school_id="+school.ID+"&task_id="+task+"&day="+day, nil, issued)
	if rec.Code != 200 {
		t.Fatalf("capacity status = %d: %s", rec.Code, rec.Body.String())
	}
	var report oapi.CapacityReport
	e.decode(rec, &report)
	if report.Demand != 81 || report.Capacity != 64 || report.Shortfall != 17 {
		t.Fatalf("报告 = demand %d capacity %d shortfall %d, want 81/64/17",
			report.Demand, report.Capacity, report.Shortfall)
	}

	plan := e.plan(t, issued, day, school.ID, task)
	if plan.Demand != 81 || len(plan.Rows) != 81 {
		t.Fatalf("计划 demand=%d rows=%d", plan.Demand, len(plan.Rows))
	}
	for _, r := range plan.Rows {
		if r.RunAt.Before(plan.WindowStart) || !r.RunAt.Before(plan.WindowEnd) {
			t.Fatalf("超容量行越窗: %v not in [%v,%v)", r.RunAt, plan.WindowStart, plan.WindowEnd)
		}
	}
}

// 手动签到：开窗入队 202；关窗 409；跨校 404；本人账号可签。
func TestP4ManualSign(t *testing.T) {
	e := w2cNewEnv(t)
	loc := p4Location(t)
	now := clock.System{}.Now()
	day := clock.BusinessDay(now, loc)
	master := e.user("p4-sign-master@example.com", coreauth.RoleMaster)
	masterIssued := e.login(master)
	owner := e.user("p4-sign-owner@example.com", coreauth.RoleUser)
	ownerIssued := e.login(owner)
	admin := e.user("p4-sign-admin@example.com", coreauth.RoleAdmin)
	adminIssued := e.login(admin)

	openSchool := e.school("p4-sign-open")
	openTask := e.schoolTask(openSchool.ID, "晨签")
	openAcct := e.account(openSchool.ID, master.ID, "13800009101")
	ownerAcct := e.account(openSchool.ID, owner.ID, "13800009102")
	e.activateAccount(openAcct)
	e.activateAccount(ownerAcct)
	if _, err := e.sched.PutWindow(e.ctx, coreschedule.WindowRecord{
		SchoolTaskID: openTask, Day: day,
		Start: now.Add(-10 * time.Minute), End: now.Add(10 * time.Minute),
		Source: string(contract.WindowPlatform),
	}, 0); err != nil {
		t.Fatalf("put open window: %v", err)
	}

	rec := e.do("POST", "/api/accounts/"+openAcct+"/sign", nil, masterIssued)
	if rec.Code != 202 {
		t.Fatalf("开窗签到 status = %d: %s", rec.Code, rec.Body.String())
	}
	var accepted oapi.AccountSignAccepted
	e.decode(rec, &accepted)
	if accepted.TaskId == "" {
		t.Fatal("响应缺 task_id")
	}
	if state := e.scalarString("SELECT state::text FROM sign_tasks WHERE id = $1::uuid", accepted.TaskId); state != string(corestatus.TaskPending) {
		t.Fatalf("任务状态 = %q, want pending", state)
	}

	rec = e.do("POST", "/api/accounts/"+ownerAcct+"/sign", nil, ownerIssued)
	if rec.Code != 202 {
		t.Fatalf("本人签到 status = %d: %s", rec.Code, rec.Body.String())
	}

	// 跨校管理员：读不到该账号，必须 404 且不入队。
	bindSchool := e.school("p4-sign-bound")
	e.bind(admin.ID, bindSchool.ID)
	rec = e.do("POST", "/api/accounts/"+openAcct+"/sign", nil, adminIssued)
	if rec.Code != 404 {
		t.Fatalf("跨校签到 status = %d: %s", rec.Code, rec.Body.String())
	}

	// 关窗：另一所学校，窗口已结束。
	closedSchool := e.school("p4-sign-closed")
	closedTask := e.schoolTask(closedSchool.ID, "晨签")
	closedAcct := e.account(closedSchool.ID, master.ID, "13800009103")
	e.activateAccount(closedAcct)
	if _, err := e.sched.PutWindow(e.ctx, coreschedule.WindowRecord{
		SchoolTaskID: closedTask, Day: day,
		Start: now.Add(-2 * time.Hour), End: now.Add(-time.Minute),
		Source: string(contract.WindowPlatform),
	}, 0); err != nil {
		t.Fatalf("put closed window: %v", err)
	}
	rec = e.do("POST", "/api/accounts/"+closedAcct+"/sign", nil, masterIssued)
	if rec.Code != 409 {
		t.Fatalf("关窗签到 status = %d: %s", rec.Code, rec.Body.String())
	}
	if body := rec.Body.String(); !strings.Contains(body, "window_closed") {
		t.Fatalf("关窗响应 = %s", body)
	}
}

// 人工覆盖优先于平台窗口，并重排 pending 行。
func TestP4OverrideRearranges(t *testing.T) {
	e := w2cNewEnv(t)
	loc := p4Location(t)
	day := clock.BusinessDay(clock.System{}.Now(), loc)
	master := e.user("p4-override-master@example.com", coreauth.RoleMaster)
	issued := e.login(master)
	school := e.school("p4-override")
	task := e.schoolTask(school.ID, "晨签")
	for _, phone := range []string{"13800009201", "13800009202"} {
		id := e.account(school.ID, master.ID, phone)
		e.activateAccount(id)
	}
	start, end := windowAt(day, loc, [2]int{6, 30}, [2]int{7, 50})
	e.windows[task] = contract.Window{Start: start, End: end, Source: contract.WindowPlatform}
	e.plan(t, issued, day, school.ID, task)

	ovStart, ovEnd := windowAt(day, loc, [2]int{9, 0}, [2]int{9, 30})
	body := map[string]any{
		"day": day, "school_id": school.ID, "task_id": task,
		"start": ovStart.Format(time.RFC3339), "end": ovEnd.Format(time.RFC3339),
		"reason": "平台窗口错误",
	}
	rec := e.do("POST", "/api/schedule/override", body, issued)
	if rec.Code != 200 {
		t.Fatalf("override status = %d: %s", rec.Code, rec.Body.String())
	}
	var plan oapi.SchedulePlan
	e.decode(rec, &plan)
	if plan.Source != "override" || !plan.WindowStart.Equal(ovStart) || !plan.WindowEnd.Equal(ovEnd) {
		t.Fatalf("覆盖后计划 = source %s [%v,%v]", plan.Source, plan.WindowStart, plan.WindowEnd)
	}
	early := e.scalarString("SELECT to_char(min(run_at), 'YYYY-MM-DD\"T\"HH24:MI:SS') FROM sign_tasks WHERE school_task_id = $1::uuid AND day = $2::date", task, day)
	want := ovStart.UTC().Format("2006-01-02T15:04:05")
	if early != want {
		t.Fatalf("最早 run_at = %s, want %s（UTC）", early, want)
	}
	if src := e.scalarString("SELECT source FROM window_cache WHERE school_task_id = $1::uuid AND day = $2::date", task, day); src != "override" {
		t.Fatalf("window_cache 来源 = %q, want override", src)
	}

	// 理由为空：400。
	body["reason"] = "  "
	rec = e.do("POST", "/api/schedule/override", body, issued)
	if rec.Code != 400 {
		t.Fatalf("空理由 status = %d: %s", rec.Code, rec.Body.String())
	}
}

// 替换已有覆盖：pending 行整体落进新窗，已了结行不动。
//
// 用户动作：管理员发现上次覆盖也不对，再改一次当日窗口。
// 断言的性质：任何 pending 行的落点都在新窗内；当日已有结论的行
// 保持原落点与状态；覆盖只留最后一条。
func TestP4OverrideReplacementReplansPending(t *testing.T) {
	e := w2cNewEnv(t)
	loc := p4Location(t)
	day := clock.BusinessDay(clock.System{}.Now(), loc)
	master := e.user("p4-override-replace@example.com", coreauth.RoleMaster)
	issued := e.login(master)
	school := e.school("p4-override-replace")
	task := e.schoolTask(school.ID, "晨签")
	accts := make([]string, 0, 3)
	for _, phone := range []string{"13800009301", "13800009302", "13800009303"} {
		id := e.account(school.ID, master.ID, phone)
		e.activateAccount(id)
		accts = append(accts, id)
	}
	start, end := windowAt(day, loc, [2]int{6, 30}, [2]int{7, 50})
	e.windows[task] = contract.Window{Start: start, End: end, Source: contract.WindowPlatform}
	e.plan(t, issued, day, school.ID, task)

	firstStart, firstEnd := windowAt(day, loc, [2]int{9, 0}, [2]int{9, 30})
	e.override(t, issued, day, school.ID, task, firstStart, firstEnd, "平台窗口错误")
	firstVersion := e.scalarInt("SELECT version FROM window_cache WHERE school_task_id = $1::uuid AND day = $2::date", task, day)

	// 一个当日已了结的行：认领后落终态，模拟"今天已经签过"。
	doneRow := e.scalarString("SELECT id::text FROM sign_tasks WHERE account_id = $1::uuid AND school_task_id = $2::uuid AND day = $3::date", accts[0], task, day)
	now := clock.System{}.Now()
	if err := e.sched.ClaimPending(e.ctx, accts[0], task, day, now); err != nil {
		t.Fatalf("claim concluded row: %v", err)
	}
	if err := e.sched.Finish(e.ctx, accts[0], task, day, corestatus.TaskSuccess, string(contract.OutcomeSuccess), now); err != nil {
		t.Fatalf("finish concluded row: %v", err)
	}
	doneAt := e.scalarString("SELECT to_char(run_at, 'YYYY-MM-DD\"T\"HH24:MI:SS') FROM sign_tasks WHERE id = $1::uuid", doneRow)

	// 第二次覆盖替换第一次：窗更窄。
	secondStart, secondEnd := windowAt(day, loc, [2]int{10, 0}, [2]int{10, 20})
	plan := e.override(t, issued, day, school.ID, task, secondStart, secondEnd, "平台窗口再次订正")
	if plan.Source != "override" || !plan.WindowStart.Equal(secondStart) || !plan.WindowEnd.Equal(secondEnd) {
		t.Fatalf("替换后计划 = source %s [%v,%v]", plan.Source, plan.WindowStart, plan.WindowEnd)
	}
	if n := e.scalarInt(`SELECT count(*) FROM sign_tasks
		WHERE school_task_id = $1::uuid AND day = $2::date AND state = 'pending'`, task, day); n != 2 {
		t.Fatalf("pending 行数 = %d, want 2", n)
	}
	if n := e.scalarInt(`SELECT count(*) FROM sign_tasks
		WHERE school_task_id = $1::uuid AND day = $2::date AND state = 'pending'
		  AND (run_at < $3 OR run_at >= $4)`, task, day, secondStart, secondEnd); n != 0 {
		t.Fatalf("%d 行 pending 落点越出新窗 [%v,%v]", n, secondStart, secondEnd)
	}
	if got := e.scalarString("SELECT to_char(run_at, 'YYYY-MM-DD\"T\"HH24:MI:SS') FROM sign_tasks WHERE id = $1::uuid", doneRow); got != doneAt {
		t.Fatalf("已了结行 run_at = %s, want %s（结论不可改写）", got, doneAt)
	}
	if got := e.scalarString("SELECT state::text FROM sign_tasks WHERE id = $1::uuid", doneRow); got != string(corestatus.TaskSuccess) {
		t.Fatalf("已了结行状态 = %q, want success", got)
	}
	if n := e.scalarInt("SELECT count(*) FROM window_overrides WHERE school_task_id = $1::uuid AND day = $2::date", task, day); n != 1 {
		t.Fatalf("覆盖行数 = %d, want 1（同键只留最后一条）", n)
	}
	if reason := e.scalarString("SELECT reason FROM window_overrides WHERE school_task_id = $1::uuid AND day = $2::date", task, day); reason != "平台窗口再次订正" {
		t.Fatalf("覆盖理由 = %q, want 最后一条", reason)
	}
	if src := e.scalarString("SELECT source FROM window_cache WHERE school_task_id = $1::uuid AND day = $2::date", task, day); src != "override" {
		t.Fatalf("window_cache 来源 = %q, want override", src)
	}
	if v := e.scalarInt("SELECT version FROM window_cache WHERE school_task_id = $1::uuid AND day = $2::date", task, day); v <= firstVersion {
		t.Fatalf("换窗后版本 = %d, want > %d（版本推进是重排的判据）", v, firstVersion)
	}
}

// 平台发现失败：计划照常落库，窗口来源如实标 fallback。
//
// 用户动作：管理员在平台不可用时建当日计划。
// 断言的性质：计划不因发现失败而失败；落库来源与实际采用的窗口一致。
func TestP4FallbackSourceRecorded(t *testing.T) {
	e := w2cNewEnv(t)
	loc := p4Location(t)
	day := clock.BusinessDay(clock.System{}.Now(), loc)
	master := e.user("p4-fallback-master@example.com", coreauth.RoleMaster)
	issued := e.login(master)
	school := e.school("p4-fallback")
	task := e.schoolTask(school.ID, "晨签")
	for _, phone := range []string{"13800009401", "13800009402"} {
		id := e.account(school.ID, master.ID, phone)
		e.activateAccount(id)
	}
	// 不预置窗口：发现替身回 not_found，走回退窗口。

	plan := e.plan(t, issued, day, school.ID, task)
	fbStart, fbEnd := windowAt(day, loc, [2]int{6, 30}, [2]int{7, 50})
	if plan.Source != "fallback" {
		t.Fatalf("计划来源 = %q, want fallback（发现失败必须如实标注）", plan.Source)
	}
	if !plan.WindowStart.Equal(fbStart) || !plan.WindowEnd.Equal(fbEnd) {
		t.Fatalf("回退窗口 = [%v,%v], want [%v,%v]", plan.WindowStart, plan.WindowEnd, fbStart, fbEnd)
	}
	if src := e.scalarString("SELECT source FROM window_cache WHERE school_task_id = $1::uuid AND day = $2::date", task, day); src != "fallback" {
		t.Fatalf("window_cache 来源 = %q, want fallback", src)
	}
	if n := e.scalarInt("SELECT count(*) FROM sign_tasks WHERE school_task_id = $1::uuid AND day = $2::date", task, day); n != 2 {
		t.Fatalf("落库计划行数 = %d, want 2", n)
	}
	if n := e.scalarInt(`SELECT count(*) FROM sign_tasks
		WHERE school_task_id = $1::uuid AND day = $2::date
		  AND (run_at < $3 OR run_at >= $4)`, task, day, fbStart, fbEnd); n != 0 {
		t.Fatalf("%d 行落点越出回退窗 [%v,%v]", n, fbStart, fbEnd)
	}
}

// override 发一次人工覆盖请求并解出计划响应。
func (e *w2cEnv) override(t *testing.T, issued *coreauth.Issued, day, schoolID, taskID string, start, end time.Time, reason string) oapi.SchedulePlan {
	t.Helper()
	body := map[string]any{
		"day": day, "school_id": schoolID, "task_id": taskID,
		"start": start.Format(time.RFC3339), "end": end.Format(time.RFC3339),
		"reason": reason,
	}
	rec := e.do("POST", "/api/schedule/override", body, issued)
	if rec.Code != 200 {
		t.Fatalf("override status = %d: %s", rec.Code, rec.Body.String())
	}
	var plan oapi.SchedulePlan
	e.decode(rec, &plan)
	return plan
}

// 自选时间片：写入、读取、冷却与角色边界。
func TestP4TimePref(t *testing.T) {
	e := w2cNewEnv(t)
	user := e.user("p4-pref@example.com", coreauth.RoleUser)
	issued := e.login(user)

	rec := e.do("PUT", "/api/me/time-pref", map[string]any{"offset_min": 30}, issued)
	if rec.Code != 200 {
		t.Fatalf("set status = %d: %s", rec.Code, rec.Body.String())
	}
	var pref oapi.TimePref
	e.decode(rec, &pref)
	if pref.OffsetMin == nil || *pref.OffsetMin != 30 || !pref.Allowed {
		t.Fatalf("set body = %+v", pref)
	}

	rec = e.do("GET", "/api/me/time-pref", nil, issued)
	e.decode(rec, &pref)
	if pref.OffsetMin == nil || *pref.OffsetMin != 30 {
		t.Fatalf("get body = %+v", pref)
	}

	// 冷却期内切换：429。
	rec = e.do("PUT", "/api/me/time-pref", map[string]any{"offset_min": 45}, issued)
	if rec.Code != 429 {
		t.Fatalf("冷却期 status = %d: %s", rec.Code, rec.Body.String())
	}

	// 粒度非法：400（校验先于冷却）。
	rec = e.do("PUT", "/api/me/time-pref", map[string]any{"offset_min": 7}, issued)
	if rec.Code != 400 {
		t.Fatalf("非法粒度 status = %d: %s", rec.Code, rec.Body.String())
	}

	// master 无权使用自选端点。
	master := e.user("p4-pref-master@example.com", coreauth.RoleMaster)
	masterIssued := e.login(master)
	rec = e.do("GET", "/api/me/time-pref", nil, masterIssued)
	if rec.Code != 403 {
		t.Fatalf("master status = %d: %s", rec.Code, rec.Body.String())
	}
}

// plan 发一次建计划请求并解出响应。
func (e *w2cEnv) plan(t *testing.T, issued *coreauth.Issued, day, schoolID, taskID string) oapi.SchedulePlan {
	t.Helper()
	body := map[string]string{"day": day, "school_id": schoolID, "task_id": taskID}
	rec := e.do("POST", "/api/schedule/plan", body, issued)
	if rec.Code != 200 {
		t.Fatalf("plan status = %d: %s", rec.Code, rec.Body.String())
	}
	var plan oapi.SchedulePlan
	e.decode(rec, &plan)
	return plan
}
