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
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
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
	m8cInsertTaskAt(t, e, accountID, taskID, day, state, coreclock.System{}.Now().UTC())
}

// m8cInsertTaskAt 写入指定的 updated_at：同账号多任务时用它钉住「最后更新」。
func m8cInsertTaskAt(t *testing.T, e *w2cEnv, accountID, taskID, day, state string, at time.Time) {
	t.Helper()
	if _, err := e.db.Exec(e.ctx, `
		INSERT INTO sign_tasks (account_id, school_task_id, day, run_at, state, updated_at)
		VALUES ($1::uuid, $2::uuid, $3::date, $4, $5::sign_task_state, $4)`,
		accountID, taskID, day, at, state); err != nil {
		t.Fatalf("insert sign task: %v", err)
	}
}

// m8cActivate 把账号置为 active：自选人数与签到统计只认活跃账号。
func m8cActivate(t *testing.T, e *w2cEnv, accountID string) {
	t.Helper()
	if _, err := e.db.Exec(e.ctx,
		`UPDATE accounts SET status = 'active' WHERE id = $1::uuid`, accountID); err != nil {
		t.Fatalf("activate account: %v", err)
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
	masterFirst := e.account(schoolA.ID, master.ID, "13800000040")
	adminAccount := e.account(schoolA.ID, admin.ID, "13800000041")
	// 同一用户两个账号只计一次。
	masterSecond := e.account(schoolA.ID, master.ID, "13800000050")
	m8cActivate(t, e, masterSecond)
	m8cActivate(t, e, masterFirst)
	m8cActivate(t, e, adminAccount)
	// 待审账号不计入：它的用户即使选了 offset 0，首槽仍只应有 1 人。
	pendingUser := e.user("m8c-pref-pending@example.com", coreauth.RoleUser)
	pendingAccount := e.account(schoolA.ID, pendingUser.ID, "13800000060")
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
		{pendingUser.ID, 0},
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
	// 拥挤度按选中任务过滤（P2-13）：给当日任务 A 铺计划行。
	// master 两个账号都铺，仍只按用户计一次；待审账号即使有行也被状态过滤。
	day := m8cDay(0)
	if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{
		{AccountID: masterFirst, SchoolTaskID: taskA, Day: day, RunAt: start},
		{AccountID: masterSecond, SchoolTaskID: taskA, Day: day, RunAt: start},
		{AccountID: adminAccount, SchoolTaskID: taskA, Day: day, RunAt: start},
		{AccountID: pendingAccount, SchoolTaskID: taskA, Day: day, RunAt: start},
	}); err != nil {
		t.Fatalf("seed plan rows: %v", err)
	}

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

// 同账号同日两个任务：只取最后更新的一条，日桶合计等于账号数。
func TestM8CSignStatsDedupesMultipleTasks(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("m8c-dedupe@example.com", coreauth.RoleMaster)
	school := e.school("m8c-dedupe")
	account := e.account(school.ID, master.ID, "13800000040")
	taskA, err := e.sched.CreateSchoolTask(e.ctx, school.ID, "m8c-dedupe-a")
	if err != nil {
		t.Fatalf("create task A: %v", err)
	}
	taskB, err := e.sched.CreateSchoolTask(e.ctx, school.ID, "m8c-dedupe-b")
	if err != nil {
		t.Fatalf("create task B: %v", err)
	}
	first := time.Date(2026, 11, 2, 6, 0, 0, 0, m8cZone)
	m8cInsertTaskAt(t, e, account, taskA, m8cDay(0), string(corestatus.TaskSuccess), first)
	m8cInsertTaskAt(t, e, account, taskB, m8cDay(0), string(corestatus.TaskFailed), first.Add(10*time.Minute))

	got := m8cGetStats(t, e, "/api/sign-stats?days=1", e.login(master), http.StatusOK)
	if got.Total != 1 {
		t.Fatalf("total = %d, want 1", got.Total)
	}
	if got.ByStatus.Success != 0 || got.ByStatus.Failed != 1 {
		t.Fatalf("by_status = %+v, want only the last updated failed row", got.ByStatus)
	}
	if len(got.Days) != 1 || got.Days[0].Success != 0 || got.Days[0].Failed != 1 {
		t.Fatalf("day bucket = %+v, want one failed account", got.Days)
	}
	if got.Days[0].Success+got.Days[0].Failed+got.Days[0].Skipped+
		got.Days[0].Pending+got.Days[0].Running != got.Total {
		t.Fatalf("day bucket sum %+v must equal total %d", got.Days[0], got.Total)
	}
}

// 槽数按整 5 分钟片（floor），最多两小时；非 5 分钟整数倍窗口不得多出尾槽。
func TestM8CTimePrefSlotBoundary(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("m8c-slot@example.com", coreauth.RoleMaster)
	school := e.school("m8c-slot")
	e.account(school.ID, master.ID, "13800000040")
	short, err := e.sched.CreateSchoolTask(e.ctx, school.ID, "m8c-slot-short")
	if err != nil {
		t.Fatalf("create short task: %v", err)
	}
	long, err := e.sched.CreateSchoolTask(e.ctx, school.ID, "m8c-slot-long")
	if err != nil {
		t.Fatalf("create long task: %v", err)
	}
	tiny, err := e.sched.CreateSchoolTask(e.ctx, school.ID, "m8c-slot-tiny")
	if err != nil {
		t.Fatalf("create tiny task: %v", err)
	}
	nowLocal := coreclock.System{}.Now().In(m8cZone)
	base := time.Date(nowLocal.Year(), nowLocal.Month(), nowLocal.Day(), 6, 30, 0, 0, m8cZone)
	e.windows[short] = contract.Window{Start: base, End: base.Add(28 * time.Minute), Source: contract.WindowPlatform}
	e.windows[long] = contract.Window{Start: base, End: base.Add(121 * time.Minute), Source: contract.WindowPlatform}
	e.windows[tiny] = contract.Window{Start: base, End: base.Add(4 * time.Minute), Source: contract.WindowPlatform}
	session := e.login(master)
	// F5：GET stats 只读缓存，不发现平台。先用 POST refresh 把三个窗口落缓存。
	for _, task := range []string{short, long, tiny} {
		rec := e.do(http.MethodPost,
			"/api/capacity/refresh?school_id="+school.ID+"&task_id="+task, nil, session)
		w2cWantStatus(t, rec, http.StatusOK)
	}

	for _, tc := range []struct {
		task  string
		slots int
	}{
		{short, 5},
		{long, 24},
		{tiny, 1},
	} {
		rec := e.do(http.MethodGet,
			"/api/time-prefs/stats?school_id="+school.ID+"&task_id="+tc.task, nil, session)
		w2cWantStatus(t, rec, http.StatusOK)
		var got m8cPrefStats
		e.decode(rec, &got)
		if len(got.Slots) != tc.slots {
			t.Fatalf("task %s slots = %d, want %d", tc.task, len(got.Slots), tc.slots)
		}
	}
}
