package main

// K0Y e2e：停用任务的行为（k0y-6）与窗口发现节流（k0y-4）。
//
// # 功能
// 用真实 PostgreSQL 与内存会话直接向路由发请求。
// k0y-6 钉三态：停用任务可预览；建计划与写覆盖回 409；
// 启用后同一请求 200。
// k0y-4 钉平台发现计数：有缓存时默认 GET 零增量；
// refresh=true 才触发发现；无缓存时首次发现并落缓存；
// 覆盖优先逻辑不变。
//
// # 归属
// cmd/server 的 e2e。复用 w2cEnv 的 schema 隔离与 HTTP 客户端。
//
// # 口径
// 平台调用次数取 e2eDiscoverer.count()：这是风控暴露面的验收口径。
// 缺 DSN 时 w2cNewEnv 打可见横幅再跳过。

import (
	"net/http"
	"strings"
	"testing"
	"time"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// k0yPlanBody 是预览响应的可辨识面：enabled 与窗口起止。
// 按 JSON 字段解码，不依赖生成的 Go 类型：钉的是线上契约。
type k0yPlanBody struct {
	Enabled     *bool   `json:"enabled"`
	Source      string  `json:"source"`
	WindowStart *string `json:"window_start"`
	WindowEnd   *string `json:"window_end"`
}

// k0yPreview 发一次预览请求并解出可辨识面。
func (e *w2cEnv) k0yPreview(issued *coreauth.Issued, day, schoolID, taskID string) k0yPlanBody {
	e.t.Helper()
	path := "/api/schedule/preview?day=" + day + "&school_id=" + schoolID + "&task_id=" + taskID
	rec := e.do(http.MethodGet, path, nil, issued)
	if rec.Code != http.StatusOK {
		e.t.Fatalf("preview status = %d, want 200: %s", rec.Code, rec.Body.String())
	}
	var body k0yPlanBody
	e.decode(rec, &body)
	return body
}

// 停用任务：可预览；建计划与写覆盖回 409；启用后同一请求成功。
func TestK0y6DisabledTask(t *testing.T) {
	e := w2cNewEnv(t)
	loc := p4Location(t)
	day := clock.BusinessDay(clock.System{}.Now(), loc)
	master := e.user("k0y6-master@example.com", coreauth.RoleMaster)
	issued := e.login(master)
	school := e.school("k0y6")
	task := e.schoolTask(school.ID, "晨签")
	acct := e.account(school.ID, master.ID, "13800009301")
	e.activateAccount(acct)
	start, end := windowAt(day, loc, [2]int{6, 30}, [2]int{7, 50})
	e.windows[task] = contract.Window{Start: start, End: end, Source: contract.WindowPlatform}

	// 启用态基线：预览带 enabled 字段且为真。
	prev := e.k0yPreview(issued, day, school.ID, task)
	if prev.Enabled == nil {
		t.Fatal("preview 响应缺 enabled 字段：停用态无法辨识")
	}
	if !*prev.Enabled {
		t.Fatal("启用任务预览 enabled=false")
	}

	if _, err := e.db.Exec(e.ctx,
		"UPDATE school_tasks SET enabled = false WHERE id = $1::uuid", task); err != nil {
		t.Fatalf("disable the task: %v", err)
	}

	// ① 停用任务仍可预览：200、enabled=false、原窗口可见。
	prev = e.k0yPreview(issued, day, school.ID, task)
	if prev.Enabled == nil || *prev.Enabled {
		t.Fatalf("停用任务预览 enabled = %v, want false", prev.Enabled)
	}
	if prev.WindowStart == nil || prev.WindowEnd == nil {
		t.Fatalf("停用任务预览丢了窗口: %+v", prev)
	}
	if *prev.WindowStart != start.Format(time.RFC3339) || *prev.WindowEnd != end.Format(time.RFC3339) {
		t.Fatalf("停用任务预览窗口 = [%s, %s], want [%v, %v]",
			*prev.WindowStart, *prev.WindowEnd, start, end)
	}

	// ② 建计划：409 conflict，消息点名停用，且一行都不落库。
	// 拒绝路径不得打平台发现：停用任务不再消耗风控暴露面。
	base := e.discover.count()
	planBody := map[string]string{"day": day, "school_id": school.ID, "task_id": task}
	rec := e.do(http.MethodPost, "/api/schedule/plan", planBody, issued)
	if rec.Code != http.StatusConflict {
		t.Fatalf("停用任务建计划 status = %d, want 409: %s", rec.Code, rec.Body.String())
	}
	if code := e.errorCode(rec); code != "conflict" {
		t.Fatalf("停用任务建计划 code = %q, want conflict", code)
	}
	if body := rec.Body.String(); !strings.Contains(body, "停用") {
		t.Fatalf("停用任务建计划消息未点名停用: %s", body)
	}
	if n := e.scalarInt("SELECT count(*) FROM sign_tasks WHERE school_task_id = $1::uuid", task); n != 0 {
		t.Fatalf("停用任务建计划落库行数 = %d, want 0", n)
	}

	// ③ 写覆盖：409 conflict，消息点名停用，且覆盖未落库。
	ovStart, ovEnd := windowAt(day, loc, [2]int{9, 0}, [2]int{9, 30})
	ovBody := map[string]any{
		"day": day, "school_id": school.ID, "task_id": task,
		"start": ovStart.Format(time.RFC3339), "end": ovEnd.Format(time.RFC3339),
		"reason": "平台窗口错误",
	}
	rec = e.do(http.MethodPost, "/api/schedule/override", ovBody, issued)
	if rec.Code != http.StatusConflict {
		t.Fatalf("停用任务写覆盖 status = %d, want 409: %s", rec.Code, rec.Body.String())
	}
	if code := e.errorCode(rec); code != "conflict" {
		t.Fatalf("停用任务写覆盖 code = %q, want conflict", code)
	}
	if body := rec.Body.String(); !strings.Contains(body, "停用") {
		t.Fatalf("停用任务写覆盖消息未点名停用: %s", body)
	}
	if n := e.scalarInt("SELECT count(*) FROM window_overrides WHERE school_task_id = $1::uuid", task); n != 0 {
		t.Fatalf("停用任务写覆盖落库行数 = %d, want 0", n)
	}
	if got := e.discover.count() - base; got != 0 {
		t.Fatalf("停用任务的两个拒绝端点打了平台发现 %d 次, want 0", got)
	}

	// ④ 启用后同一请求 200（回归）。
	if _, err := e.db.Exec(e.ctx,
		"UPDATE school_tasks SET enabled = true WHERE id = $1::uuid", task); err != nil {
		t.Fatalf("re-enable the task: %v", err)
	}
	plan := e.plan(t, issued, day, school.ID, task)
	if plan.Source != "platform" || !plan.WindowStart.Equal(start) {
		t.Fatalf("启用后计划 = source %s start %v", plan.Source, plan.WindowStart)
	}
	rec = e.do(http.MethodPost, "/api/schedule/override", ovBody, issued)
	if rec.Code != http.StatusOK {
		t.Fatalf("启用后写覆盖 status = %d, want 200: %s", rec.Code, rec.Body.String())
	}
	if n := e.scalarInt("SELECT count(*) FROM window_overrides WHERE school_task_id = $1::uuid", task); n != 1 {
		t.Fatalf("启用后覆盖落库行数 = %d, want 1", n)
	}
}

// 有缓存时默认 GET 零平台调用；refresh=true 才触发发现。
func TestK0y4DiscoveryThrottled(t *testing.T) {
	e := w2cNewEnv(t)
	loc := p4Location(t)
	day := clock.BusinessDay(clock.System{}.Now(), loc)
	master := e.user("k0y4-master@example.com", coreauth.RoleMaster)
	issued := e.login(master)
	school := e.school("k0y4")
	task := e.schoolTask(school.ID, "晨签")
	acct := e.account(school.ID, master.ID, "13800009401")
	e.activateAccount(acct)
	start, end := windowAt(day, loc, [2]int{6, 30}, [2]int{7, 50})
	e.windows[task] = contract.Window{Start: start, End: end, Source: contract.WindowPlatform}

	capacityPath := "/api/capacity?school_id=" + school.ID + "&task_id=" + task + "&day=" + day
	windowsPath := "/api/schedule/windows?day=" + day

	// ① 无缓存：首次默认 GET 发现一次，并把窗口落进当日缓存。
	base := e.discover.count()
	rec := e.do(http.MethodGet, capacityPath, nil, issued)
	if rec.Code != http.StatusOK {
		t.Fatalf("capacity status = %d: %s", rec.Code, rec.Body.String())
	}
	if got := e.discover.count() - base; got != 1 {
		t.Fatalf("无缓存首次 GET 平台调用 = %d, want 1", got)
	}
	if n := e.scalarInt(
		"SELECT count(*) FROM window_cache WHERE school_task_id = $1::uuid AND day = $2::date",
		task, day); n != 1 {
		t.Fatalf("首次 GET 后缓存行数 = %d, want 1", n)
	}
	var report oapi.CapacityReport
	e.decode(rec, &report)
	if report.Source != "platform" {
		t.Fatalf("首次报告来源 = %s, want platform", report.Source)
	}

	// ② 有缓存：连续两次默认 GET（两个端点各一次）平台调用零增量。
	base = e.discover.count()
	if rec := e.do(http.MethodGet, capacityPath, nil, issued); rec.Code != http.StatusOK {
		t.Fatalf("cached capacity status = %d: %s", rec.Code, rec.Body.String())
	}
	if rec := e.do(http.MethodGet, windowsPath, nil, issued); rec.Code != http.StatusOK {
		t.Fatalf("cached windows status = %d: %s", rec.Code, rec.Body.String())
	}
	if got := e.discover.count() - base; got != 0 {
		t.Fatalf("有缓存时两次默认 GET 平台调用 = %d, want 0", got)
	}

	// ③ refresh=true 触发发现：两个端点各 +1。
	base = e.discover.count()
	if rec := e.do(http.MethodGet, capacityPath+"&refresh=true", nil, issued); rec.Code != http.StatusOK {
		t.Fatalf("capacity refresh status = %d: %s", rec.Code, rec.Body.String())
	}
	if got := e.discover.count() - base; got != 1 {
		t.Fatalf("capacity refresh=true 平台调用 = %d, want 1", got)
	}
	base = e.discover.count()
	rec = e.do(http.MethodGet, windowsPath+"&refresh=true", nil, issued)
	if rec.Code != http.StatusOK {
		t.Fatalf("windows refresh status = %d: %s", rec.Code, rec.Body.String())
	}
	if got := e.discover.count() - base; got != 1 {
		t.Fatalf("windows refresh=true 平台调用 = %d, want 1", got)
	}
	var list oapi.ScheduleWindowList
	e.decode(rec, &list)
	if len(list.Items) != 1 || !list.Items[0].Enabled || list.Items[0].Start == nil {
		t.Fatalf("刷新后的窗口列表 = %+v", list.Items)
	}

	// ④ 覆盖优先：写覆盖后默认 GET 报 override，平台调用零增量。
	ovStart, ovEnd := windowAt(day, loc, [2]int{9, 0}, [2]int{9, 30})
	rec = e.do(http.MethodPost, "/api/schedule/override", map[string]any{
		"day": day, "school_id": school.ID, "task_id": task,
		"start": ovStart.Format(time.RFC3339), "end": ovEnd.Format(time.RFC3339),
		"reason": "平台窗口错误",
	}, issued)
	if rec.Code != http.StatusOK {
		t.Fatalf("override status = %d: %s", rec.Code, rec.Body.String())
	}
	base = e.discover.count()
	rec = e.do(http.MethodGet, capacityPath, nil, issued)
	if rec.Code != http.StatusOK {
		t.Fatalf("override capacity status = %d: %s", rec.Code, rec.Body.String())
	}
	if got := e.discover.count() - base; got != 0 {
		t.Fatalf("覆盖存在时默认 GET 平台调用 = %d, want 0", got)
	}
	e.decode(rec, &report)
	if report.Source != "override" || !report.WindowStart.Equal(ovStart) {
		t.Fatalf("覆盖后报告 = source %s start %v", report.Source, report.WindowStart)
	}

	// ⑤ 覆盖存在时 refresh=true 仍发现平台窗口，但不改写缓存行。
	// 依据：缓存行是 worker 的窗口判据（只读缓存）。改写它会让覆盖失效，
	// pending 行可能按平台窗口判终态（skipped），当日不可恢复。
	// 报告与计划仍以覆盖为准；两个只读端点的 refresh 行为一致。
	base = e.discover.count()
	rec = e.do(http.MethodGet, capacityPath+"&refresh=true", nil, issued)
	if rec.Code != http.StatusOK {
		t.Fatalf("覆盖态 refresh status = %d: %s", rec.Code, rec.Body.String())
	}
	if got := e.discover.count() - base; got != 1 {
		t.Fatalf("覆盖态 refresh=true 平台调用 = %d, want 1", got)
	}
	if src := e.scalarString(
		"SELECT source FROM window_cache WHERE school_task_id = $1::uuid AND day = $2::date",
		task, day); src != "override" {
		t.Fatalf("覆盖态 refresh 后缓存来源 = %q, want override（缓存行不得被改写）", src)
	}
	e.decode(rec, &report)
	if report.Source != "override" || !report.WindowStart.Equal(ovStart) || !report.WindowEnd.Equal(ovEnd) {
		t.Fatalf("覆盖态 refresh 报告 = source %s [%v,%v], want override [%v,%v]",
			report.Source, report.WindowStart, report.WindowEnd, ovStart, ovEnd)
	}

	// 窗口列表的 refresh 同判据：发现照做，缓存行不改写。
	base = e.discover.count()
	rec = e.do(http.MethodGet, windowsPath+"&refresh=true", nil, issued)
	if rec.Code != http.StatusOK {
		t.Fatalf("覆盖态 windows refresh status = %d: %s", rec.Code, rec.Body.String())
	}
	if got := e.discover.count() - base; got != 1 {
		t.Fatalf("覆盖态 windows refresh=true 平台调用 = %d, want 1", got)
	}
	if src := e.scalarString(
		"SELECT source FROM window_cache WHERE school_task_id = $1::uuid AND day = $2::date",
		task, day); src != "override" {
		t.Fatalf("覆盖态 windows refresh 后缓存来源 = %q, want override（缓存行不得被改写）", src)
	}
}
