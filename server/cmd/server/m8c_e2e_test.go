package main

// M8C 看板聚合 e2e：签到终态统计与自选时间片人数。
//
// # 功能
// 钉住四条用户可见性质：
//   1. 签到统计按作用域收窄：master 全量、admin 只见绑定校、user 403。
//   2. 日桶互斥、按日计数；缺失日期补零并列入 days_missing。
//   3. 窗口参数 1..90，越界 400。
//   4. 自选时间片人数按用户去重，槽覆盖有效窗口且最多两小时；跨校 404。
//
// # 归属
// cmd/server 用完整路由与真实 PostgreSQL。
//
// # 口径
// 缺 DSN 时 w2cNewEnv 显式跳过。日期按 CST（与测试装配的 Location 同源）。

import (
	"net/http"
	"testing"
	"time"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

type m8cStatsDay struct {
	Day     string `json:"day"`
	Pending int    `json:"pending"`
	Running int    `json:"running"`
	Success int    `json:"success"`
	Failed  int    `json:"failed"`
	Skipped int    `json:"skipped"`
}

type m8cStats struct {
	Total    int `json:"total"`
	ByStatus struct {
		Pending int `json:"pending"`
		Running int `json:"running"`
		Success int `json:"success"`
		Failed  int `json:"failed"`
		Skipped int `json:"skipped"`
	} `json:"by_status"`
	Days        []m8cStatsDay `json:"days"`
	DaysMissing []string      `json:"days_missing"`
}

type m8cSlot struct {
	OffsetMin int    `json:"offset_min"`
	Label     string `json:"label"`
	Count     int    `json:"count"`
	Cap       int    `json:"cap"`
}

type m8cPrefStats struct {
	SchoolID    string    `json:"school_id"`
	TaskID      string    `json:"task_id"`
	Day         string    `json:"day"`
	WindowStart time.Time `json:"window_start"`
	WindowEnd   time.Time `json:"window_end"`
	Slots       []m8cSlot `json:"slots"`
}

var m8cZone = time.FixedZone("CST", 8*3600)

// m8cDay 返回相对今天偏移 n 天的 CST 日期。
func m8cDay(n int) string {
	return coreclock.System{}.Now().In(m8cZone).AddDate(0, 0, n).Format("2006-01-02")
}

// m8cInsertTask 直接写一行签到任务，用作统计的终态载体。
func m8cInsertTask(t *testing.T, e *w2cEnv, accountID, taskID, day, state string) {
	t.Helper()
	if _, err := e.db.Exec(e.ctx, `
		INSERT INTO sign_tasks (account_id, school_task_id, day, run_at, state)
		VALUES ($1::uuid, $2::uuid, $3::date, $4, $5::sign_task_state)`,
		accountID, taskID, day, coreclock.System{}.Now().UTC(), state); err != nil {
		t.Fatalf("insert sign task: %v", err)
	}
}

// m8cGetStats 发一次统计请求并解出响应。
func m8cGetStats(t *testing.T, e *w2cEnv, path string, issued *coreauth.Issued, want int) m8cStats {
	t.Helper()
	rec := e.do(http.MethodGet, path, nil, issued)
	w2cWantStatus(t, rec, want)
	var out m8cStats
	if want == http.StatusOK {
		e.decode(rec, &out)
	}
	return out
}

// 签到统计按作用域收窄；日桶互斥。
func TestM8CSignStatsScope(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("m8c-master@example.com", coreauth.RoleMaster)
	admin := e.user("m8c-admin@example.com", coreauth.RoleAdmin)
	user := e.user("m8c-user@example.com", coreauth.RoleUser)
	schoolA := e.school("m8c-a")
	schoolB := e.school("m8c-b")
	e.bind(admin.ID, schoolA.ID)
	a1 := e.account(schoolA.ID, master.ID, "13800000040")
	a2 := e.account(schoolA.ID, admin.ID, "13800000041")
	b1 := e.account(schoolB.ID, master.ID, "13800000050")
	taskA, err := e.sched.CreateSchoolTask(e.ctx, schoolA.ID, "m8c-task-a")
	if err != nil {
		t.Fatalf("create task A: %v", err)
	}
	taskB, err := e.sched.CreateSchoolTask(e.ctx, schoolB.ID, "m8c-task-b")
	if err != nil {
		t.Fatalf("create task B: %v", err)
	}
	today, yesterday := m8cDay(0), m8cDay(-1)
	m8cInsertTask(t, e, a1, taskA, today, string(corestatus.TaskSuccess))
	m8cInsertTask(t, e, a2, taskA, today, string(corestatus.TaskFailed))
	m8cInsertTask(t, e, a1, taskA, yesterday, string(corestatus.TaskSuccess))
	m8cInsertTask(t, e, b1, taskB, today, string(corestatus.TaskSuccess))

	got := m8cGetStats(t, e, "/api/sign-stats?days=2", e.login(master), http.StatusOK)
	if got.Total != 3 {
		t.Fatalf("master total = %d, want 3", got.Total)
	}
	if got.ByStatus.Success != 2 || got.ByStatus.Failed != 1 {
		t.Fatalf("master by_status = %+v, want success 2 / failed 1", got.ByStatus)
	}
	if len(got.Days) != 2 {
		t.Fatalf("days = %d, want 2", len(got.Days))
	}
	if got.Days[0].Day != yesterday || got.Days[0].Success != 1 {
		t.Fatalf("yesterday bucket = %+v", got.Days[0])
	}
	if got.Days[1].Success != 2 || got.Days[1].Failed != 1 {
		t.Fatalf("today bucket = %+v, want success 2 / failed 1", got.Days[1])
	}
	if len(got.DaysMissing) != 0 {
		t.Fatalf("days_missing = %v, want empty", got.DaysMissing)
	}

	adminStats := m8cGetStats(t, e, "/api/sign-stats?days=2", e.login(admin), http.StatusOK)
	if adminStats.Total != 2 || adminStats.ByStatus.Success != 1 || adminStats.ByStatus.Failed != 1 {
		t.Fatalf("admin stats = %+v, want only school A", adminStats)
	}

	m8cGetStats(t, e, "/api/sign-stats?days=2", e.login(user), http.StatusForbidden)
}

// 缺失日期补零并列入 days_missing；窗口越界 400。
func TestM8CSignStatsWindow(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("m8c-window@example.com", coreauth.RoleMaster)
	school := e.school("m8c-window")
	account := e.account(school.ID, master.ID, "13800000060")
	task, err := e.sched.CreateSchoolTask(e.ctx, school.ID, "m8c-window-task")
	if err != nil {
		t.Fatalf("create task: %v", err)
	}
	m8cInsertTask(t, e, account, task, m8cDay(0), string(corestatus.TaskSkipped))

	got := m8cGetStats(t, e, "/api/sign-stats?days=3", e.login(master), http.StatusOK)
	if len(got.Days) != 3 || len(got.DaysMissing) != 2 {
		t.Fatalf("days=%d missing=%v, want 3 days and 2 missing", len(got.Days), got.DaysMissing)
	}
	if got.Days[0].Day != m8cDay(-2) || got.Days[0].Skipped != 0 {
		t.Fatalf("missing day must be zero-filled: %+v", got.Days[0])
	}
	if got.Days[2].Skipped != 1 {
		t.Fatalf("today bucket = %+v, want skipped 1", got.Days[2])
	}

	m8cGetStats(t, e, "/api/sign-stats?days=0", e.login(master), http.StatusBadRequest)
	m8cGetStats(t, e, "/api/sign-stats?days=91", e.login(master), http.StatusBadRequest)
}

// 自选时间片人数按用户去重，槽覆盖有效窗口；跨校任务 404。
func TestM8CTimePrefStats(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("m8c-pref-master@example.com", coreauth.RoleMaster)
	admin := e.user("m8c-pref-admin@example.com", coreauth.RoleAdmin)
	schoolA := e.school("m8c-pref-a")
	schoolB := e.school("m8c-pref-b")
	e.bind(admin.ID, schoolA.ID)
	e.account(schoolA.ID, master.ID, "13800000040")
	e.account(schoolA.ID, admin.ID, "13800000041")
	// 同一用户两个账号只计一次。
	e.account(schoolA.ID, master.ID, "13800000050")
	taskA, err := e.sched.CreateSchoolTask(e.ctx, schoolA.ID, "m8c-pref-task-a")
	if err != nil {
		t.Fatalf("create task A: %v", err)
	}
	taskB, err := e.sched.CreateSchoolTask(e.ctx, schoolB.ID, "m8c-pref-task-b")
	if err != nil {
		t.Fatalf("create task B: %v", err)
	}
	for _, pref := range []struct {
		userID string
		offset int
	}{
		{master.ID, 0},
		{admin.ID, 10},
	} {
		if _, err := e.db.Exec(e.ctx, `
			INSERT INTO time_prefs (user_id, offset_min) VALUES ($1::uuid, $2)
			ON CONFLICT (user_id) DO UPDATE SET offset_min = EXCLUDED.offset_min`,
			pref.userID, pref.offset); err != nil {
			t.Fatalf("seed time pref: %v", err)
		}
	}
	nowLocal := coreclock.System{}.Now().In(m8cZone)
	start := time.Date(nowLocal.Year(), nowLocal.Month(), nowLocal.Day(), 6, 30, 0, 0, m8cZone)
	e.windows[taskA] = contract.Window{Start: start, End: start.Add(80 * time.Minute), Source: contract.WindowPlatform}

	rec := e.do(http.MethodGet,
		"/api/time-prefs/stats?school_id="+schoolA.ID+"&task_id="+taskA, nil, e.login(master))
	w2cWantStatus(t, rec, http.StatusOK)
	var got m8cPrefStats
	e.decode(rec, &got)
	if got.Day != m8cDay(0) {
		t.Fatalf("day = %q, want today", got.Day)
	}
	if len(got.Slots) != 16 {
		t.Fatalf("slots = %d, want 16 for an 80 minute window", len(got.Slots))
	}
	if got.Slots[0].Label != "06:30" || got.Slots[0].Count != 1 {
		t.Fatalf("first slot = %+v, want 06:30 with one user", got.Slots[0])
	}
	if got.Slots[2].Label != "06:40" || got.Slots[2].Count != 1 {
		t.Fatalf("offset 10 slot = %+v, want 06:40 with one user", got.Slots[2])
	}
	if got.Slots[0].Cap != 5 {
		t.Fatalf("cap = %d, want 5", got.Slots[0].Cap)
	}

	// admin 绑定 A：跨校任务 404。
	rec = e.do(http.MethodGet,
		"/api/time-prefs/stats?school_id="+schoolB.ID+"&task_id="+taskB, nil, e.login(admin))
	w2cWantStatus(t, rec, http.StatusNotFound)

	// user 无权。
	user := e.user("m8c-pref-user@example.com", coreauth.RoleUser)
	rec = e.do(http.MethodGet,
		"/api/time-prefs/stats?school_id="+schoolA.ID+"&task_id="+taskA, nil, e.login(user))
	w2cWantStatus(t, rec, http.StatusForbidden)
}
