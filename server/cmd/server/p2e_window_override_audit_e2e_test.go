package main

// P2-3 全栈：人工覆盖与审计链同一事务。
//
// 判据 1：两次真 HTTP override → 恰好两条 schedule.window_override；
// 第二条 changes 的旧值等于第一条的新值；覆盖表只留一行；链 Verify 通过。
// 判据 2：审计 INSERT 失败（临时重命名 audit_logs）→ POST 返回 5xx，
// 覆盖行、窗口缓存与计划行零变化，审计行数不变；恢复后重试成功且只多一条。

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// p2eAuditCount 数指定动作的审计行。
func p2eAuditCount(e *w2cEnv, action coreaudit.Action) int64 {
	return e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = $1", string(action))
}

// p2eAuditChange 取第 offset 条覆盖审计里 changes 的一个路径。
// 缺失的旧值以字符串 null 返回：NULL 会让 QueryString 扫不动。
// path 只来自本文件常量，不来自请求。
func p2eAuditChange(e *w2cEnv, path string, offset int64) string {
	return e.scalarString("SELECT coalesce(detail::jsonb #>> '{"+path+"}', 'null')"+" FROM audit_logs WHERE action = $1 ORDER BY id OFFSET $2 LIMIT 1",
		string(coreaudit.ActionScheduleWindowOverride), offset)
}

// p2eOverrideRequest 发一次覆盖请求并原样返回响应，供失败码断言。
func p2eOverrideRequest(e *w2cEnv, issued *coreauth.Issued, day, schoolID, taskID string,
	start, end time.Time, reason string) *httptest.ResponseRecorder {
	body := map[string]any{
		"day": day, "school_id": schoolID, "task_id": taskID,
		"start": start.Format(time.RFC3339), "end": end.Format(time.RFC3339),
		"reason": reason,
	}
	return e.do(http.MethodPost, "/api/schedule/override", body, issued)
}

func TestP2EWindowOverrideWritesAuditInTx(t *testing.T) {
	e := w2cNewEnv(t)
	loc := p4Location(t)
	day := clock.BusinessDay(clock.System{}.Now(), loc)
	master := e.user("p2e-override-audit@example.com", coreauth.RoleMaster)
	issued := e.login(master)
	school := e.school("p2e-override-audit")
	task := e.schoolTask(school.ID, "晨签")
	e.activateAccount(e.account(school.ID, master.ID, "13800009401"))
	baseStart, baseEnd := windowAt(day, loc, [2]int{6, 30}, [2]int{7, 50})
	e.windows[task] = contract.Window{Start: baseStart, End: baseEnd, Source: contract.WindowPlatform}
	e.plan(t, issued, day, school.ID, task)

	firstStart, firstEnd := windowAt(day, loc, [2]int{9, 0}, [2]int{9, 30})
	e.override(t, issued, day, school.ID, task, firstStart, firstEnd, "平台窗口错误")
	secondStart, secondEnd := windowAt(day, loc, [2]int{10, 0}, [2]int{10, 20})
	e.override(t, issued, day, school.ID, task, secondStart, secondEnd, "平台窗口再次订正")

	action := coreaudit.ActionScheduleWindowOverride
	if n := p2eAuditCount(e, action); n != 2 {
		t.Fatalf("覆盖审计行数 = %d, want 2（每次真写覆盖一条）", n)
	}
	epoch := func(v time.Time) string { return strconv.FormatInt(v.Unix(), 10) }
	// 第一次覆盖：旧值必须为 nil，不得拿请求里的新值冒充旧值。
	for _, path := range []string{"changes,window.start,0", "changes,window.end,0"} {
		if got := p2eAuditChange(e, path, 0); got != "null" {
			t.Fatalf("第一条 %s 旧值 = %q, want null", path, got)
		}
	}
	for path, want := range map[string]string{
		"changes,window.start,1": epoch(firstStart),
		"changes,window.end,1":   epoch(firstEnd),
		"reason":                 "平台窗口错误",
	} {
		if got := p2eAuditChange(e, path, 0); got != want {
			t.Fatalf("第一条 %s = %q, want %q", path, got, want)
		}
	}
	// 第二次覆盖：旧值来自库内旧覆盖，新值来自本次请求。
	for path, want := range map[string]string{
		"changes,window.start,0": epoch(firstStart),
		"changes,window.start,1": epoch(secondStart),
		"changes,window.end,0":   epoch(firstEnd),
		"changes,window.end,1":   epoch(secondEnd),
		"reason":                 "平台窗口再次订正",
	} {
		if got := p2eAuditChange(e, path, 1); got != want {
			t.Fatalf("第二条 %s = %q, want %q", path, got, want)
		}
	}
	// 目标与主体：school_id 取请求校、目标 school_task、主体已掩码。
	if got := e.scalarString("SELECT school_id::text FROM audit_logs WHERE action = $1 ORDER BY id DESC LIMIT 1", string(action)); got != school.ID {
		t.Fatalf("审计 school_id = %q, want %q", got, school.ID)
	}
	if got := e.scalarString("SELECT target_type FROM audit_logs WHERE action = $1 ORDER BY id DESC LIMIT 1", string(action)); got != "school_task" {
		t.Fatalf("审计 target_type = %q, want school_task", got)
	}
	if got := e.scalarString("SELECT target_id::text FROM audit_logs WHERE action = $1 ORDER BY id DESC LIMIT 1", string(action)); got != task {
		t.Fatalf("审计 target_id = %q, want %q", got, task)
	}
	wantActor := appaudit.NewActor(coreauth.User{ID: master.ID, Email: master.Email, Role: master.Role})
	if got := e.scalarString("SELECT actor_label FROM audit_logs WHERE action = $1 ORDER BY id DESC LIMIT 1", string(action)); got != wantActor.Label {
		t.Fatalf("actor_label = %q, want %q", got, wantActor.Label)
	}
	if label := e.scalarString("SELECT actor_label FROM audit_logs WHERE action = $1 ORDER BY id DESC LIMIT 1", string(action)); strings.Contains(label, master.Email) {
		t.Fatalf("actor_label 含邮箱原文: %q", label)
	}
	// 覆盖表只留最后一行。
	if n := e.scalarInt("SELECT count(*) FROM window_overrides WHERE school_task_id = $1::uuid AND day = $2::date", task, day); n != 1 {
		t.Fatalf("覆盖行数 = %d, want 1", n)
	}
	if got := e.scalarString("SELECT reason FROM window_overrides WHERE school_task_id = $1::uuid AND day = $2::date", task, day); got != "平台窗口再次订正" {
		t.Fatalf("覆盖理由 = %q", got)
	}
	if got := e.scalarInt("SELECT (extract(epoch FROM start_at)::bigint) FROM window_overrides WHERE school_task_id = $1::uuid AND day = $2::date", task, day); got != secondStart.Unix() {
		t.Fatalf("覆盖 start epoch = %d, want %d", got, secondStart.Unix())
	}
	// 链完整：Verify 覆盖全部行，巡检端点也认。
	report, err := e.audits.Verify(e.ctx, 0, 0)
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	total := e.scalarInt("SELECT count(*) FROM audit_logs")
	if report.Count != total {
		t.Fatalf("Verify 覆盖 %d 行, 表内 %d 行", report.Count, total)
	}
	status := e.auditStatus(issued)
	if !status.OK || status.Empty || status.CheckedRows != total {
		t.Fatalf("audit-status = %+v, want ok=true checked=%d", status, total)
	}
}

func TestP2EWindowOverrideRollsBackWhenAuditFails(t *testing.T) {
	e := w2cNewEnv(t)
	loc := p4Location(t)
	day := clock.BusinessDay(clock.System{}.Now(), loc)
	master := e.user("p2e-override-rollback@example.com", coreauth.RoleMaster)
	issued := e.login(master)
	school := e.school("p2e-override-rollback")
	task := e.schoolTask(school.ID, "晨签")
	e.activateAccount(e.account(school.ID, master.ID, "13800009402"))
	baseStart, baseEnd := windowAt(day, loc, [2]int{6, 30}, [2]int{7, 50})
	e.windows[task] = contract.Window{Start: baseStart, End: baseEnd, Source: contract.WindowPlatform}
	e.plan(t, issued, day, school.ID, task)

	firstStart, firstEnd := windowAt(day, loc, [2]int{9, 0}, [2]int{9, 30})
	e.override(t, issued, day, school.ID, task, firstStart, firstEnd, "第一次订正")

	// 快照：覆盖行、缓存、计划行与审计行数。
	ovEpoch := func(col string) int64 {
		return e.scalarInt("SELECT (extract(epoch FROM "+col+")::bigint) FROM window_overrides WHERE school_task_id = $1::uuid AND day = $2::date", task, day)
	}
	startBefore := ovEpoch("start_at")
	endBefore := ovEpoch("end_at")
	reasonBefore := e.scalarString("SELECT reason FROM window_overrides WHERE school_task_id = $1::uuid AND day = $2::date", task, day)
	cacheStartBefore := e.scalarInt("SELECT (extract(epoch FROM start_at)::bigint) FROM window_cache WHERE school_task_id = $1::uuid AND day = $2::date", task, day)
	cacheVersionBefore := e.scalarInt("SELECT version FROM window_cache WHERE school_task_id = $1::uuid AND day = $2::date", task, day)
	plansBefore := e.scalarInt("SELECT count(*) FROM sign_tasks WHERE school_task_id = $1::uuid AND day = $2::date", task, day)
	earliestBefore := e.scalarString(`SELECT to_char(min(run_at), 'YYYY-MM-DD"T"HH24:MI:SS') FROM sign_tasks WHERE school_task_id = $1::uuid AND day = $2::date`, task, day)
	auditsBefore := e.scalarInt("SELECT count(*) FROM audit_logs")

	broken := e.schema + ".audit_logs_p2e_broken"
	if _, err := e.db.Exec(e.ctx, "ALTER TABLE "+e.schema+".audit_logs RENAME TO audit_logs_p2e_broken"); err != nil {
		t.Fatalf("rename audit_logs: %v", err)
	}
	t.Cleanup(func() {
		// 断言中途失败也要把表名还原；已经还原时这里的错误可忽略。
		_, _ = e.db.Exec(context.Background(),
			"ALTER TABLE IF EXISTS "+broken+" RENAME TO audit_logs")
	})

	secondStart, secondEnd := windowAt(day, loc, [2]int{10, 0}, [2]int{10, 30})
	rec := p2eOverrideRequest(e, issued, day, school.ID, task, secondStart, secondEnd, "审计不可用时不得写入")
	if rec.Code < 500 {
		t.Fatalf("审计失败 status = %d, want 5xx: %s", rec.Code, rec.Body.String())
	}
	// 覆盖行零变化。
	if n := e.scalarInt("SELECT count(*) FROM window_overrides WHERE school_task_id = $1::uuid AND day = $2::date", task, day); n != 1 {
		t.Fatalf("失败后覆盖行数 = %d, want 1", n)
	}
	if got := ovEpoch("start_at"); got != startBefore {
		t.Fatalf("失败后覆盖 start = %d, want %d", got, startBefore)
	}
	if got := ovEpoch("end_at"); got != endBefore {
		t.Fatalf("失败后覆盖 end = %d, want %d", got, endBefore)
	}
	if got := e.scalarString("SELECT reason FROM window_overrides WHERE school_task_id = $1::uuid AND day = $2::date", task, day); got != reasonBefore {
		t.Fatalf("失败后理由 = %q, want %q", got, reasonBefore)
	}
	// 窗口缓存零变化。
	if got := e.scalarInt("SELECT (extract(epoch FROM start_at)::bigint) FROM window_cache WHERE school_task_id = $1::uuid AND day = $2::date", task, day); got != cacheStartBefore {
		t.Fatalf("失败后缓存 start = %d, want %d", got, cacheStartBefore)
	}
	if got := e.scalarInt("SELECT version FROM window_cache WHERE school_task_id = $1::uuid AND day = $2::date", task, day); got != cacheVersionBefore {
		t.Fatalf("失败后缓存 version = %d, want %d", got, cacheVersionBefore)
	}
	// 计划行零变化（数量与最早落点）。
	if got := e.scalarInt("SELECT count(*) FROM sign_tasks WHERE school_task_id = $1::uuid AND day = $2::date", task, day); got != plansBefore {
		t.Fatalf("失败后计划行 = %d, want %d", got, plansBefore)
	}
	if got := e.scalarString(`SELECT to_char(min(run_at), 'YYYY-MM-DD"T"HH24:MI:SS') FROM sign_tasks WHERE school_task_id = $1::uuid AND day = $2::date`, task, day); got != earliestBefore {
		t.Fatalf("失败后最早 run_at = %s, want %s", got, earliestBefore)
	}
	// 审计行数不变：失败事务不得留下半条链。
	if got := e.scalarInt("SELECT count(*) FROM " + broken); got != auditsBefore {
		t.Fatalf("失败后审计行 = %d, want %d", got, auditsBefore)
	}

	// 恢复后重试：成功且审计恰好多一条。
	if _, err := e.db.Exec(e.ctx, "ALTER TABLE "+broken+" RENAME TO audit_logs"); err != nil {
		t.Fatalf("restore audit_logs: %v", err)
	}
	thirdStart, thirdEnd := windowAt(day, loc, [2]int{11, 0}, [2]int{11, 30})
	e.override(t, issued, day, school.ID, task, thirdStart, thirdEnd, "恢复后的订正")
	if n := p2eAuditCount(e, coreaudit.ActionScheduleWindowOverride); n != auditsBefore+1 {
		t.Fatalf("恢复后覆盖审计行 = %d, want %d", n, auditsBefore+1)
	}
	if got := e.scalarString("SELECT reason FROM window_overrides WHERE school_task_id = $1::uuid AND day = $2::date", task, day); got != "恢复后的订正" {
		t.Fatalf("恢复后理由 = %q", got)
	}
	if _, err := e.audits.Verify(e.ctx, 0, 0); err != nil {
		t.Fatalf("恢复后 Verify: %v", err)
	}
}
