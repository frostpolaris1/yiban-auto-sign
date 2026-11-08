package main

// 用户读面 e2e：签到日历、自选窗口与照片保留期读取。
//
// # 功能
// 用真实 PostgreSQL 与内存会话直接向路由发请求，钉住：
// 日历跨日计数、账号终态去重、软删与跨用户排除、31 天边界、
// 日详情所有权、time-pref 窗口/槽位、photo-retention 回显与角色矩阵。
//
// # 归属
// cmd/server 的 e2e。复用 w2cEnv 的 schema 隔离、造数与 HTTP 客户端。
//
// # 口径
// 缺 DSN 时由 w2cRequireDSN 打可见横幅并跳过，绝不静默当绿。
// 业务日取服务端业务时区；测试不假设运行机器的本地时区。

import (
	"math"
	"net/http"
	"strings"
	"testing"
	"time"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
)

// userReadLoc 是 w2cEnv 装配时使用的业务时区。
func userReadLoc() *time.Location { return time.FixedZone("CST", 8*3600) }

// userReadDay 返回相对 base 偏移 delta 天的 YYYY-MM-DD。
func userReadDay(t *testing.T, base string, delta int) string {
	t.Helper()
	day, err := time.ParseInLocation("2006-01-02", base, userReadLoc())
	if err != nil {
		t.Fatalf("parse day %s: %v", base, err)
	}
	return day.AddDate(0, 0, delta).Format("2006-01-02")
}

// userReadAt 返回业务日当天指定时分秒的时刻。
func userReadAt(t *testing.T, day string, hour, minute, second int) time.Time {
	t.Helper()
	d, err := time.ParseInLocation("2006-01-02", day, userReadLoc())
	if err != nil {
		t.Fatalf("parse day %s: %v", day, err)
	}
	return time.Date(d.Year(), d.Month(), d.Day(), hour, minute, second, 0, userReadLoc())
}

// seedTask 直接落一行 sign_tasks；测试用它构造跨日与多行同日的数据。
func (e *w2cEnv) seedTask(accountID, schoolTaskID, day string, state corestatus.TaskState,
	attempts int, runAt, updatedAt time.Time) {
	e.t.Helper()
	_, err := e.db.Exec(e.ctx,
		`INSERT INTO sign_tasks (account_id, school_task_id, day, run_at, state, attempts, updated_at)
		 VALUES ($1::uuid, $2::uuid, $3::date, $4, $5::sign_task_state, $6, $7)`,
		accountID, schoolTaskID, day, runAt, string(state), attempts, updatedAt)
	if err != nil {
		e.t.Fatalf("seed sign task (%s, %s, %s): %v", accountID, schoolTaskID, day, err)
	}
}

// TestSignCalendarRangeAndDetail 覆盖跨日、账号去重、软删与所有权。
func TestSignCalendarRangeAndDetail(t *testing.T) {
	e := w2cNewEnv(t)
	loc := userReadLoc()
	now := clock.System{}.Now()
	today := clock.BusinessDay(now, loc)
	yesterday := userReadDay(t, today, -1)
	twoDaysAgo := userReadDay(t, today, -2)
	future := userReadDay(t, today, 1)

	u1 := e.user("user-read-range-u1@example.com", coreauth.RoleUser)
	u2 := e.user("user-read-range-u2@example.com", coreauth.RoleUser)
	issued := e.login(u1)
	school := e.school("user-read-range")
	task1 := e.schoolTask(school.ID, "晨签")
	task2 := e.schoolTask(school.ID, "晚签")

	acctA := e.account(school.ID, u1.ID, "13800000001")
	acctB := e.account(school.ID, u1.ID, "13800000002")
	acctC := e.account(school.ID, u1.ID, "13800000003")
	acctOther := e.account(school.ID, u2.ID, "13800000004")
	acctDeleted := e.account(school.ID, u1.ID, "13800000005")
	if _, err := e.db.Exec(e.ctx,
		"UPDATE accounts SET deleted_at = now() WHERE id = $1::uuid", acctDeleted); err != nil {
		t.Fatalf("soft delete account: %v", err)
	}

	latestUpdate := userReadAt(t, today, 7, 0, 0)
	// 甲：今天成功、昨天失败、前天跳过。
	e.seedTask(acctA, task1, today, corestatus.TaskSuccess, 1,
		userReadAt(t, today, 6, 35, 0), userReadAt(t, today, 6, 35, 0))
	e.seedTask(acctA, task1, yesterday, corestatus.TaskFailed, 2,
		userReadAt(t, yesterday, 6, 40, 0), userReadAt(t, yesterday, 6, 40, 0))
	e.seedTask(acctA, task1, twoDaysAgo, corestatus.TaskSkipped, 1,
		userReadAt(t, twoDaysAgo, 6, 40, 0), userReadAt(t, twoDaysAgo, 6, 40, 0))
	// 乙：同一天两条成功行，按账号只算一次。
	e.seedTask(acctB, task1, today, corestatus.TaskSuccess, 1,
		userReadAt(t, today, 6, 31, 0), userReadAt(t, today, 6, 31, 0))
	e.seedTask(acctB, task2, today, corestatus.TaskSuccess, 1,
		userReadAt(t, today, 6, 32, 0), userReadAt(t, today, 6, 32, 0))
	// 丙：pending 归入 unknown；它的更新时间是范围内最大值。
	e.seedTask(acctC, task1, today, corestatus.TaskPending, 0,
		userReadAt(t, today, 7, 0, 0), latestUpdate)
	// 他人账号：任何计数都不得包含。
	e.seedTask(acctOther, task1, today, corestatus.TaskSuccess, 1,
		userReadAt(t, today, 6, 33, 0), userReadAt(t, today, 6, 33, 0))
	// 软删账号：即使有终态行也不得出现。
	e.seedTask(acctDeleted, task1, today, corestatus.TaskFailed, 1,
		userReadAt(t, today, 6, 34, 0), userReadAt(t, today, 6, 34, 0))
	// 未来日：库里即使有行，范围计数也必须清零，日详情也不得列出。
	e.seedTask(acctA, task1, future, corestatus.TaskPending, 0,
		userReadAt(t, future, 5, 0, 0), userReadAt(t, today, 4, 0, 0))

	rec := e.do(http.MethodGet, "/api/me/sign-calendar?from="+twoDaysAgo+"&to="+future, nil, issued)
	if rec.Code != http.StatusOK {
		t.Fatalf("calendar status = %d: %s", rec.Code, rec.Body.String())
	}
	var report oapi.SignCalendar
	e.decode(rec, &report)
	if report.Today != today {
		t.Fatalf("today = %q, want %q", report.Today, today)
	}
	if report.Timezone != loc.String() {
		t.Fatalf("timezone = %q, want %q", report.Timezone, loc.String())
	}
	if !report.UpdatedAt.Equal(latestUpdate) {
		t.Fatalf("updated_at = %v, want %v", report.UpdatedAt, latestUpdate)
	}
	if len(report.Days) != 4 {
		t.Fatalf("days = %d, want 4: %+v", len(report.Days), report.Days)
	}
	want := []oapi.SignCalendarDay{
		{Day: twoDaysAgo, Total: 1, Skipped: 1},
		{Day: yesterday, Total: 1, Failed: 1},
		{Day: today, Total: 3, Success: 2, Unknown: 1},
		{Day: future, Future: true},
	}
	for i, day := range report.Days {
		if day != want[i] {
			t.Fatalf("day[%d] = %+v, want %+v", i, day, want[i])
		}
	}

	detail := e.do(http.MethodGet, "/api/me/sign-calendar/"+today, nil, issued)
	if detail.Code != http.StatusOK {
		t.Fatalf("day detail status = %d: %s", detail.Code, detail.Body.String())
	}
	var day oapi.SignCalendarDayDetail
	e.decode(detail, &day)
	if day.Day != today || day.Timezone != loc.String() {
		t.Fatalf("day detail header = %+v", day)
	}
	if day.Future {
		t.Fatal("今天不应标为 future")
	}
	states := map[string]oapi.SignCalendarAccountState{}
	for _, item := range day.Accounts {
		states[item.AccountId] = item.State
	}
	if len(day.Accounts) != 3 {
		t.Fatalf("day accounts = %d, want 3: %+v", len(day.Accounts), day.Accounts)
	}
	if states[acctA] != oapi.SignCalendarAccountStateSuccess ||
		states[acctB] != oapi.SignCalendarAccountStateSuccess ||
		states[acctC] != oapi.SignCalendarAccountStateUnknown {
		t.Fatalf("states = %+v", states)
	}
	if _, ok := states[acctOther]; ok {
		t.Fatal("日详情包含了他人账号")
	}
	if _, ok := states[acctDeleted]; ok {
		t.Fatal("日详情包含了软删账号")
	}
	for _, item := range day.Accounts {
		if item.RunAt == nil {
			t.Fatalf("account %s 缺 run_at", item.AccountId)
		}
	}
	if got := day.Accounts[0].Attempts + day.Accounts[1].Attempts + day.Accounts[2].Attempts; got != 2 {
		t.Fatalf("attempts 合计 = %d, want 2（甲 1 + 乙 1 + 丙 0）", got)
	}

	empty := e.do(http.MethodGet, "/api/me/sign-calendar/"+userReadDay(t, today, -10), nil, issued)
	if empty.Code != http.StatusOK {
		t.Fatalf("empty day status = %d: %s", empty.Code, empty.Body.String())
	}
	var emptyDay oapi.SignCalendarDayDetail
	e.decode(empty, &emptyDay)
	if emptyDay.Accounts == nil || len(emptyDay.Accounts) != 0 {
		t.Fatalf("空日 accounts = %#v, want 空数组", emptyDay.Accounts)
	}

	futureDetail := e.do(http.MethodGet, "/api/me/sign-calendar/"+future, nil, issued)
	if futureDetail.Code != http.StatusOK {
		t.Fatalf("future day status = %d: %s", futureDetail.Code, futureDetail.Body.String())
	}
	var futureDay oapi.SignCalendarDayDetail
	e.decode(futureDetail, &futureDay)
	if !futureDay.Future || futureDay.Accounts == nil || len(futureDay.Accounts) != 0 {
		t.Fatalf("未来日详情 = %+v, want future=true 且 accounts=[]", futureDay)
	}
}

// TestSignCalendarRangeBoundsAndRoles 覆盖 31 天边界、非法输入与角色矩阵。
func TestSignCalendarRangeBoundsAndRoles(t *testing.T) {
	e := w2cNewEnv(t)
	loc := userReadLoc()
	today := clock.BusinessDay(clock.System{}.Now(), loc)
	user := e.user("user-read-bounds-user@example.com", coreauth.RoleUser)
	master := e.user("user-read-bounds-master@example.com", coreauth.RoleMaster)
	admin := e.user("user-read-bounds-admin@example.com", coreauth.RoleAdmin)

	userIssued := e.login(user)
	before := clock.System{}.Now()
	rec := e.do(http.MethodGet,
		"/api/me/sign-calendar?from="+userReadDay(t, today, -30)+"&to="+today, nil, userIssued)
	after := clock.System{}.Now()
	if rec.Code != http.StatusOK {
		t.Fatalf("31 天范围 status = %d: %s", rec.Code, rec.Body.String())
	}
	var report oapi.SignCalendar
	e.decode(rec, &report)
	if len(report.Days) != 31 {
		t.Fatalf("31 天范围 days = %d, want 31", len(report.Days))
	}
	// 范围内无任务行：updated_at 必须回落服务端 now，不能是零值。
	if report.UpdatedAt.IsZero() ||
		report.UpdatedAt.Before(before.Add(-time.Minute)) ||
		report.UpdatedAt.After(after.Add(time.Minute)) {
		t.Fatalf("空范围 updated_at = %v, want 服务端 now（%v..%v）",
			report.UpdatedAt, before, after)
	}

	cases := []struct {
		name string
		path string
	}{
		{"32 天", "/api/me/sign-calendar?from=" + userReadDay(t, today, -31) + "&to=" + today},
		{"倒挂", "/api/me/sign-calendar?from=" + today + "&to=" + userReadDay(t, today, -1)},
		{"非法日期", "/api/me/sign-calendar?from=2026-02-30&to=" + today},
		{"缺 from", "/api/me/sign-calendar?to=" + today},
		{"缺 to", "/api/me/sign-calendar?from=" + today},
		{"日详情非法", "/api/me/sign-calendar/2026-13-01"},
		{"0000 范围", "/api/me/sign-calendar?from=0000-01-01&to=0000-01-31"},
		{"0000 日详情", "/api/me/sign-calendar/0000-01-01"},
	}
	for _, tc := range cases {
		if got := e.do(http.MethodGet, tc.path, nil, userIssued).Code; got != http.StatusBadRequest {
			t.Errorf("%s status = %d, want 400", tc.name, got)
		}
	}

	// 0001 是 Postgres 可接受的边界年：两端点都必须 200，空数据。
	for _, path := range []string{
		"/api/me/sign-calendar?from=0001-01-01&to=0001-01-31",
		"/api/me/sign-calendar/0001-01-01",
	} {
		if got := e.do(http.MethodGet, path, nil, userIssued).Code; got != http.StatusOK {
			t.Errorf("0001 边界 %s status = %d, want 200", path, got)
		}
	}

	if got := e.do(http.MethodGet, "/api/me/sign-calendar?from="+today+"&to="+today, nil, nil).Code; got != http.StatusUnauthorized {
		t.Errorf("匿名 status = %d, want 401", got)
	}
	if got := e.do(http.MethodGet, "/api/me/sign-calendar?from="+today+"&to="+today, nil, e.login(master)).Code; got != http.StatusForbidden {
		t.Errorf("master status = %d, want 403", got)
	}
	if got := e.do(http.MethodGet, "/api/me/sign-calendar?from="+today+"&to="+today, nil, e.login(admin)).Code; got != http.StatusForbidden {
		t.Errorf("admin status = %d, want 403", got)
	}
}

// TestTimePrefWindowAndSlots 覆盖窗口、槽位与 PUT 兼容语义。
func TestTimePrefWindowAndSlots(t *testing.T) {
	e := w2cNewEnv(t)
	user := e.user("user-read-pref@example.com", coreauth.RoleUser)
	issued := e.login(user)

	rec := e.do(http.MethodGet, "/api/me/time-pref", nil, issued)
	if rec.Code != http.StatusOK {
		t.Fatalf("time-pref status = %d: %s", rec.Code, rec.Body.String())
	}
	var view oapi.TimePref
	e.decode(rec, &view)
	if view.Window.Start != "06:30" || view.Window.End != "07:50" {
		t.Fatalf("window = %+v, want 06:30~07:50", view.Window)
	}
	if view.Slots == nil || len(*view.Slots) != 16 {
		t.Fatalf("slots = %v, want 16", view.Slots)
	}
	slots := *view.Slots
	if slots[0].OffsetMin != 0 || slots[0].Label != "06:30" {
		t.Fatalf("首槽 = %+v", slots[0])
	}
	if slots[len(slots)-1].OffsetMin != 75 || slots[len(slots)-1].Label != "07:45" {
		t.Fatalf("末槽 = %+v", slots[len(slots)-1])
	}
	for _, slot := range slots {
		if slot.Disabled || slot.Reason != nil {
			t.Fatalf("开启态槽位不应 disabled: %+v", slot)
		}
	}
	if view.Allowed != true || view.OffsetMin != nil {
		t.Fatalf("初始 view = %+v", view)
	}

	offset := 10
	put := e.do(http.MethodPut, "/api/me/time-pref", map[string]any{"offset_min": offset}, issued)
	if put.Code != http.StatusOK {
		t.Fatalf("PUT status = %d: %s", put.Code, put.Body.String())
	}
	var stored oapi.TimePref
	e.decode(put, &stored)
	if stored.OffsetMin == nil || *stored.OffsetMin != offset {
		t.Fatalf("PUT view = %+v", stored)
	}
	if stored.Window != view.Window || stored.Slots == nil || len(*stored.Slots) != 16 {
		t.Fatalf("PUT 未保持窗口/槽位: %+v", stored)
	}

	after := e.do(http.MethodGet, "/api/me/time-pref", nil, issued)
	var reread oapi.TimePref
	e.decode(after, &reread)
	if reread.OffsetMin == nil || *reread.OffsetMin != offset {
		t.Fatalf("GET after PUT = %+v", reread)
	}
}

// TestPhotoRetentionGet 覆盖缺省 90、写后回显与角色矩阵。
func TestPhotoRetentionGet(t *testing.T) {
	e := w2cNewEnv(t)
	user := e.user("user-read-retention-user@example.com", coreauth.RoleUser)
	master := e.user("user-read-retention-master@example.com", coreauth.RoleMaster)
	userIssued := e.login(user)
	masterIssued := e.login(master)

	rec := e.do(http.MethodGet, "/api/me/photo-retention", nil, userIssued)
	if rec.Code != http.StatusOK {
		t.Fatalf("retention status = %d: %s", rec.Code, rec.Body.String())
	}
	var retention oapi.PhotoRetention
	e.decode(rec, &retention)
	if retention.Days != oapi.PhotoRetentionDaysN90 {
		t.Fatalf("缺省 days = %d, want 90", retention.Days)
	}

	put := e.do(http.MethodPut, "/api/me/photo-retention", map[string]any{"days": 30}, userIssued)
	if put.Code != http.StatusOK {
		t.Fatalf("PUT retention status = %d: %s", put.Code, put.Body.String())
	}
	rec = e.do(http.MethodGet, "/api/me/photo-retention", nil, userIssued)
	e.decode(rec, &retention)
	if retention.Days != oapi.PhotoRetentionDaysN30 {
		t.Fatalf("写后 days = %d, want 30", retention.Days)
	}

	if got := e.do(http.MethodGet, "/api/me/photo-retention", nil, masterIssued).Code; got != http.StatusOK {
		t.Errorf("master status = %d, want 200", got)
	}
	if got := e.do(http.MethodGet, "/api/me/photo-retention", nil, nil).Code; got != http.StatusUnauthorized {
		t.Errorf("匿名 status = %d, want 401", got)
	}
}

// seedTimePref 直接写一行自选；用户读面与 admin stats 必须读到同一份。
func (e *w2cEnv) seedTimePref(userID string, offset int) {
	e.t.Helper()
	if _, err := e.db.Exec(e.ctx, `
		INSERT INTO time_prefs (user_id, offset_min) VALUES ($1::uuid, $2)
		ON CONFLICT (user_id) DO UPDATE SET offset_min = EXCLUDED.offset_min`,
		userID, offset); err != nil {
		e.t.Fatalf("seed time pref: %v", err)
	}
}

// seedWindow 直接落当日窗口缓存：用户读面是缓存只读，必须由缓存提供窗口。
func (e *w2cEnv) seedWindow(taskID, day string, start, end time.Time) {
	e.t.Helper()
	if _, err := e.db.Exec(e.ctx, `
		INSERT INTO window_cache (school_task_id, day, start_at, end_at, source, version)
		VALUES ($1::uuid, $2::date, $3, $4, 'platform', 1)
		ON CONFLICT (school_task_id, day) DO UPDATE
		SET start_at = EXCLUDED.start_at, end_at = EXCLUDED.end_at,
		    source = EXCLUDED.source, version = window_cache.version + 1`,
		taskID, day, start, end); err != nil {
		e.t.Fatalf("seed window: %v", err)
	}
}

// 用户自选读面带出任务窗口、与 admin stats 同源的拥挤度、台账预计时段；
// 用户仍不能调用 admin stats；task_id 不下发。
func TestTimePrefCrowdingAndEstimate(t *testing.T) {
	e := w2cNewEnv(t)
	loc := userReadLoc()
	today := clock.BusinessDay(clock.System{}.Now(), loc)
	master := e.user("timepref-crowd-master@example.com", coreauth.RoleMaster)
	user := e.user("timepref-crowd-user@example.com", coreauth.RoleUser)
	peer := e.user("timepref-crowd-peer@example.com", coreauth.RoleUser)
	school := e.school("timepref-crowd")
	task := e.schoolTask(school.ID, "晨签")
	acctUser := e.account(school.ID, user.ID, "13800000070")
	acctPeer := e.account(school.ID, peer.ID, "13800000071")
	e.activateAccount(acctUser)
	e.activateAccount(acctPeer)
	start, end := windowAt(today, loc, [2]int{6, 30}, [2]int{7, 50})
	e.seedWindow(task, today, start, end)
	e.seedTimePref(user.ID, 0)
	e.seedTimePref(peer.ID, 0)
	runAt := userReadAt(t, today, 6, 42, 0)
	e.seedTask(acctUser, task, today, corestatus.TaskPending, 0, runAt, runAt)

	rec := e.do(http.MethodGet, "/api/me/time-pref", nil, e.login(user))
	w2cWantStatus(t, rec, http.StatusOK)
	if e.discover.count() != 0 {
		t.Fatalf("用户 GET 不得触发平台发现：calls = %d", e.discover.count())
	}
	var view oapi.TimePref
	e.decode(rec, &view)
	if view.Window.Start != "06:30" || view.Window.End != "07:50" {
		t.Fatalf("window = %+v, want task window 06:30~07:50", view.Window)
	}
	if view.Slots == nil || len(*view.Slots) != 16 {
		t.Fatalf("slots = %v, want 16", view.Slots)
	}
	userSlot := (*view.Slots)[0]
	if userSlot.OffsetMin != 0 || userSlot.Label != "06:30" {
		t.Fatalf("首槽 = %+v, want 06:30", userSlot)
	}
	if view.Estimated == nil || *view.Estimated != "06:42" {
		t.Fatalf("estimated = %v, want 06:42", view.Estimated)
	}
	if !strings.Contains(view.EstimateNote, "非承诺") {
		t.Fatalf("estimate_note = %q, 必须写明非承诺", view.EstimateNote)
	}
	if strings.Contains(rec.Body.String(), task) {
		t.Fatalf("用户响应泄露 task_id: %s", rec.Body.String())
	}

	// 同一任务的 admin 统计：人数/容量就是用户 pct 的输入。
	statsRec := e.do(http.MethodGet,
		"/api/time-prefs/stats?school_id="+school.ID+"&task_id="+task, nil, e.login(master))
	w2cWantStatus(t, statsRec, http.StatusOK)
	var stats m8cPrefStats
	e.decode(statsRec, &stats)
	if len(stats.Slots) != 16 || stats.Slots[0].Count != 2 || stats.Slots[0].Cap != 5 {
		t.Fatalf("stats 首槽 = %+v, want count 2 cap 5", stats.Slots)
	}
	if want := expectedCrowdPct(stats.Slots[0].Count, stats.Slots[0].Cap); userSlot.Pct != want {
		t.Fatalf("用户 pct = %d, stats count/cap 推导 = %d", userSlot.Pct, want)
	}
	if e.discover.count() != 0 {
		t.Fatalf("缓存命中时 admin stats 也不得发现：calls = %d", e.discover.count())
	}
	// 用户无权调用 admin stats。
	userStats := e.do(http.MethodGet,
		"/api/time-prefs/stats?school_id="+school.ID+"&task_id="+task, nil, e.login(user))
	w2cWantStatus(t, userStats, http.StatusForbidden)

	// PUT 写后回显同样只读缓存：不得因任意注册用户请求触发平台登录。
	putUser := e.user("timepref-crowd-put@example.com", coreauth.RoleUser)
	e.activateAccount(e.account(school.ID, putUser.ID, "13800000031"))
	putRec := e.do(http.MethodPut, "/api/me/time-pref", map[string]any{"offset_min": 10}, e.login(putUser))
	w2cWantStatus(t, putRec, http.StatusOK)
	var putView oapi.TimePref
	e.decode(putRec, &putView)
	if putView.Window.Start != "06:30" || putView.Window.End != "07:50" {
		t.Fatalf("PUT 回显 window = %+v, want 06:30~07:50", putView.Window)
	}
	if e.discover.count() != 0 {
		t.Fatalf("用户 PUT 不得触发平台发现：calls = %d", e.discover.count())
	}
}

// 多任务时取当日最先未了结的一行：窗口与 estimated 都跟这一行，
// 不随机、不向用户暴露 task_id。
func TestTimePrefPicksNextUnfinishedTask(t *testing.T) {
	e := w2cNewEnv(t)
	loc := userReadLoc()
	today := clock.BusinessDay(clock.System{}.Now(), loc)
	user := e.user("timepref-multi@example.com", coreauth.RoleUser)
	school := e.school("timepref-multi")
	morning := e.schoolTask(school.ID, "晨签")
	evening := e.schoolTask(school.ID, "晚签")
	acct := e.account(school.ID, user.ID, "13800000072")
	e.activateAccount(acct)
	morningStart, morningEnd := windowAt(today, loc, [2]int{6, 30}, [2]int{7, 50})
	eveningStart, eveningEnd := windowAt(today, loc, [2]int{7, 0}, [2]int{8, 20})
	e.seedWindow(morning, today, morningStart, morningEnd)
	e.seedWindow(evening, today, eveningStart, eveningEnd)
	// 晨签已了结，晚签待执行：读面必须选晚签。
	finished := userReadAt(t, today, 6, 35, 0)
	e.seedTask(acct, morning, today, corestatus.TaskSuccess, 1, finished, finished)
	nextAt := userReadAt(t, today, 7, 10, 0)
	e.seedTask(acct, evening, today, corestatus.TaskPending, 0, nextAt, nextAt)

	rec := e.do(http.MethodGet, "/api/me/time-pref", nil, e.login(user))
	w2cWantStatus(t, rec, http.StatusOK)
	if e.discover.count() != 0 {
		t.Fatalf("多任务读面不得触发平台发现：calls = %d", e.discover.count())
	}
	var view oapi.TimePref
	e.decode(rec, &view)
	if view.Window.Start != "07:00" || view.Window.End != "08:20" {
		t.Fatalf("window = %+v, want 晚签 07:00~08:20", view.Window)
	}
	if view.Estimated == nil || *view.Estimated != "07:10" {
		t.Fatalf("estimated = %v, want 07:10", view.Estimated)
	}
	if strings.Contains(rec.Body.String(), morning) || strings.Contains(rec.Body.String(), evening) {
		t.Fatalf("用户响应泄露 task_id: %s", rec.Body.String())
	}
}

// 无当日任务行且窗口不可发现：window 回退服务端标准窗口，estimated=null，
// estimate_note=待生成；不向平台之外的读面编造钟点。
func TestTimePrefFallbackWindowWithoutPlan(t *testing.T) {
	e := w2cNewEnv(t)
	user := e.user("timepref-fallback@example.com", coreauth.RoleUser)
	school := e.school("timepref-fallback")
	e.schoolTask(school.ID, "晨签")
	acct := e.account(school.ID, user.ID, "13800000073")
	e.activateAccount(acct)
	// 不登记缓存与 e.windows：用户读面直接回退标准窗口，且零平台发现。
	rec := e.do(http.MethodGet, "/api/me/time-pref", nil, e.login(user))
	w2cWantStatus(t, rec, http.StatusOK)
	if e.discover.count() != 0 {
		t.Fatalf("无缓存用户读面不得触发平台发现：calls = %d", e.discover.count())
	}
	var view oapi.TimePref
	e.decode(rec, &view)
	if view.Window.Start != "06:30" || view.Window.End != "07:50" {
		t.Fatalf("window = %+v, want fallback 06:30~07:50", view.Window)
	}
	if view.Estimated != nil {
		t.Fatalf("estimated = %v, want null", *view.Estimated)
	}
	if view.EstimateNote != "待生成" {
		t.Fatalf("estimate_note = %q, want 待生成", view.EstimateNote)
	}
	if view.Slots == nil || len(*view.Slots) != 16 || (*view.Slots)[0].Pct != 0 {
		t.Fatalf("slots = %v, want 16 slots with pct 0", view.Slots)
	}

	// 无当日任务行时取学校启用任务（名称序第一条），窗口跟该任务。
	loc := userReadLoc()
	today := clock.BusinessDay(clock.System{}.Now(), loc)
	userTwo := e.user("timepref-fallback-two@example.com", coreauth.RoleUser)
	schoolTwo := e.school("timepref-fallback-two")
	firstTask := e.schoolTask(schoolTwo.ID, "a-morning")
	e.schoolTask(schoolTwo.ID, "b-evening")
	firstStart, firstEnd := windowAt(today, loc, [2]int{6, 40}, [2]int{7, 40})
	e.seedWindow(firstTask, today, firstStart, firstEnd)
	acctTwo := e.account(schoolTwo.ID, userTwo.ID, "13800000209")
	e.activateAccount(acctTwo)
	rec = e.do(http.MethodGet, "/api/me/time-pref", nil, e.login(userTwo))
	w2cWantStatus(t, rec, http.StatusOK)
	var second oapi.TimePref
	e.decode(rec, &second)
	if second.Window.Start != "06:40" || second.Window.End != "07:40" {
		t.Fatalf("无任务行 window = %+v, want 首个启用任务 06:40~07:40", second.Window)
	}
	if second.Estimated != nil || second.EstimateNote != "待生成" {
		t.Fatalf("无任务行 estimated = %v note = %q", second.Estimated, second.EstimateNote)
	}

	// 待审账号不算生效账号：读面不得借它解析学校/任务。
	pendingUser := e.user("timepref-fallback-pending@example.com", coreauth.RoleUser)
	e.account(schoolTwo.ID, pendingUser.ID, "13800000031")
	pendingRec := e.do(http.MethodGet, "/api/me/time-pref", nil, e.login(pendingUser))
	w2cWantStatus(t, pendingRec, http.StatusOK)
	var pendingView oapi.TimePref
	e.decode(pendingRec, &pendingView)
	if pendingView.Window.Start != "06:30" || pendingView.Window.End != "07:50" {
		t.Fatalf("待审账号 window = %+v, want 标准窗口", pendingView.Window)
	}
	if pendingView.Estimated != nil {
		t.Fatalf("待审账号 estimated = %v, want null", *pendingView.Estimated)
	}
}

// expectedCrowdPct 是旧线 pct 口径的测试副本：10% 一档，满员 100，未满封顶 90。
func expectedCrowdPct(count, capacity int) int {
	if capacity < 1 {
		capacity = 1
	}
	if count >= capacity {
		return 100
	}
	if count <= 0 {
		return 0
	}
	pct := int(math.RoundToEven(float64(count*10)/float64(capacity))) * 10
	if pct < 10 {
		return 10
	}
	if pct > 90 {
		return 90
	}
	return pct
}
