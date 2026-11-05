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
	"net/http"
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

	acctA := e.account(school.ID, u1.ID, "13800009201")
	acctB := e.account(school.ID, u1.ID, "13800009202")
	acctC := e.account(school.ID, u1.ID, "13800009203")
	acctOther := e.account(school.ID, u2.ID, "13800009204")
	acctDeleted := e.account(school.ID, u1.ID, "13800009205")
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
	rec := e.do(http.MethodGet,
		"/api/me/sign-calendar?from="+userReadDay(t, today, -30)+"&to="+today, nil, userIssued)
	if rec.Code != http.StatusOK {
		t.Fatalf("31 天范围 status = %d: %s", rec.Code, rec.Body.String())
	}
	var report oapi.SignCalendar
	e.decode(rec, &report)
	if len(report.Days) != 31 {
		t.Fatalf("31 天范围 days = %d, want 31", len(report.Days))
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
	}
	for _, tc := range cases {
		if got := e.do(http.MethodGet, tc.path, nil, userIssued).Code; got != http.StatusBadRequest {
			t.Errorf("%s status = %d, want 400", tc.name, got)
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
