package diagnostic_test

// 诊断与轮询用例单测（P6）。
//
// 判据来自派单稿：数据源是只读查询；跨用户/跨校 404（由
// AccountSource 的可见面判据保证，本层透传）；suggestion 词表闭集
// wait|retry_now|re_arm|contact_admin（表驱动校验）；
// stale = now-updated_at > 90s（假时钟翻转）。

import (
	"context"
	"errors"
	"reflect"
	"testing"
	"time"

	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	appdiagnostic "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/diagnostic"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	corediag "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/diagnostic"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
)

var loc = time.FixedZone("CST", 8*3600)

var day = "2026-10-29"

func diagNow() time.Time { return time.Date(2026, 10, 29, 7, 0, 0, 0, loc) }

type fakeAccounts struct {
	page   appaccount.Page
	get    appaccount.Record
	getErr error
	listed []corescope.Scope
}

func (f *fakeAccounts) List(_ context.Context, scope corescope.Scope, _ appaccount.ListFilter, _ string, _ int) (appaccount.Page, error) {
	f.listed = append(f.listed, scope)
	return f.page, nil
}

func (f *fakeAccounts) Get(_ context.Context, scope corescope.Scope, _ string) (appaccount.Record, error) {
	f.listed = append(f.listed, scope)
	if f.getErr != nil {
		return appaccount.Record{}, f.getErr
	}
	return f.get, nil
}

type fakeStore struct {
	tasks   []corediag.TaskRow
	windows map[string]corediag.WindowInfo
	egress  map[string]corediag.EgressInfo
	counts  map[string]int

	gotAccountIDs []string
	gotTaskIDs    []string
	gotScope      corescope.Scope
	stateCalls    int
}

func (f *fakeStore) TodayTasks(_ context.Context, accountIDs []string, _ string) ([]corediag.TaskRow, error) {
	f.gotAccountIDs = accountIDs
	return f.tasks, nil
}

func (f *fakeStore) Windows(_ context.Context, taskIDs []string, _ string) (map[string]corediag.WindowInfo, error) {
	f.gotTaskIDs = taskIDs
	return f.windows, nil
}

func (f *fakeStore) Egresses(_ context.Context, accountIDs []string) (map[string]corediag.EgressInfo, error) {
	out := map[string]corediag.EgressInfo{}
	for _, id := range accountIDs {
		if e, ok := f.egress[id]; ok {
			out[id] = e
		}
	}
	return out, nil
}

func (f *fakeStore) StateCounts(_ context.Context, scope corescope.Scope, _ string) (map[string]int, error) {
	f.gotScope = scope
	f.stateCalls++
	return f.counts, nil
}

func newDiagService(t *testing.T, accts *fakeAccounts, st *fakeStore) *appdiagnostic.Service {
	t.Helper()
	svc, err := appdiagnostic.NewService(appdiagnostic.Deps{
		Accounts: accts, Store: st,
		Clock: coreclock.Fixed{T: diagNow()}, Location: loc,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

func acctRecord(id string) appaccount.Record {
	return appaccount.Record{ID: id, SchoolID: "school-1", OwnerUserID: "user-1"}
}

func taskRow(accountID, state, result string, attempts int, updatedAgo time.Duration) corediag.TaskRow {
	return corediag.TaskRow{
		TaskID: "t-" + accountID, SchoolTaskID: "st-1", AccountID: accountID,
		State: state, Result: result, Attempts: attempts,
		RunAt:     diagNow().Add(-time.Minute),
		UpdatedAt: diagNow().Add(-updatedAgo),
	}
}

// Mine 列本人账号并逐个补当日任务行；作用域必须是 User(uid)。
func TestMineListsOwnAccountsWithUserScope(t *testing.T) {
	accts := &fakeAccounts{page: appaccount.Page{Items: []appaccount.Record{acctRecord("a1")}}}
	row := taskRow("a1", string(corestatus.TaskPending), "wait_window_open", 0, time.Second)
	row.RunAt = diagNow().Add(time.Minute) // 排队中的行：下一次重试时刻就是 run_at
	st := &fakeStore{tasks: []corediag.TaskRow{row}}
	svc := newDiagService(t, accts, st)

	report, err := svc.Mine(context.Background(), "user-1")
	if err != nil {
		t.Fatalf("Mine: %v", err)
	}
	if len(accts.listed) != 1 || accts.listed[0].Kind() != corescope.KindUser {
		t.Fatalf("作用域形状不符：%v", accts.listed)
	}
	if !reflect.DeepEqual(st.gotAccountIDs, []string{"a1"}) {
		t.Fatalf("账号集 = %v", st.gotAccountIDs)
	}
	if len(report.Items) != 1 {
		t.Fatalf("items = %d", len(report.Items))
	}
	item := report.Items[0]
	if item.AccountID != "a1" || *item.State != string(corestatus.TaskPending) || item.Suggestion != corediag.SuggestWait {
		t.Fatalf("item = %+v", item)
	}
	if item.NextRetryAt == nil || !item.NextRetryAt.After(diagNow()) {
		t.Fatalf("next_retry_at = %v, want 未来时刻", item.NextRetryAt)
	}
	if !report.UpdatedAt.Equal(diagNow()) {
		t.Fatalf("updated_at = %v", report.UpdatedAt)
	}
}

// stale 翻转：>90s 真，≤90s 假，无行恒假。
func TestSignStatusStaleFlip(t *testing.T) {
	accts := &fakeAccounts{page: appaccount.Page{Items: []appaccount.Record{
		acctRecord("a1"), acctRecord("a2"), acctRecord("a3"),
	}}}
	st := &fakeStore{tasks: []corediag.TaskRow{
		taskRow("a1", string(corestatus.TaskPending), "", 0, 91*time.Second),
		taskRow("a2", string(corestatus.TaskSuccess), string(corestatus.TaskSuccess), 1, 89*time.Second),
	}}
	svc := newDiagService(t, accts, st)

	report, err := svc.SignStatus(context.Background(), "user-1")
	if err != nil {
		t.Fatalf("SignStatus: %v", err)
	}
	byID := map[string]appdiagnostic.StatusItem{}
	for _, it := range report.Items {
		byID[it.AccountID] = it
	}
	if !byID["a1"].Stale {
		t.Fatal("91s 未更新应为 stale")
	}
	if byID["a2"].Stale {
		t.Fatal("89s 更新不应 stale")
	}
	if byID["a3"].Stale || byID["a3"].State != nil {
		t.Fatalf("无行账号 = %+v, want stale=false/state=null", byID["a3"])
	}
}

// 单账号详版：跨用户 404 透传；窗口与出口标签在库有行时出现。
func TestAccountDetailIncludesWindowAndEgress(t *testing.T) {
	accts := &fakeAccounts{get: acctRecord("a1")}
	st := &fakeStore{
		tasks: []corediag.TaskRow{taskRow("a1", string(corestatus.TaskFailed), "rejected", 2, time.Second)},
		windows: map[string]corediag.WindowInfo{
			"st-1": {Start: diagNow().Add(-time.Hour), End: diagNow().Add(time.Hour), Source: "platform", Version: 3},
		},
		egress: map[string]corediag.EgressInfo{"a1": {Identity: "https://eg-1"}},
	}
	svc := newDiagService(t, accts, st)

	detail, err := svc.Account(context.Background(), corescope.User("user-1"), "a1")
	if err != nil {
		t.Fatalf("Account: %v", err)
	}
	if detail.Suggestion != corediag.SuggestRetryNow {
		t.Fatalf("suggestion = %q", detail.Suggestion)
	}
	if detail.Window == nil || detail.Window.Source != "platform" || detail.Window.Version != 3 {
		t.Fatalf("window = %+v", detail.Window)
	}
	if detail.Egress == nil || detail.Egress.Identity != "https://eg-1" {
		t.Fatalf("egress = %+v", detail.Egress)
	}

	// 跨用户：AccountSource 报 not_found，原样上抛。
	accts.getErr = coreerrs.ErrNotFound
	if _, err := svc.Account(context.Background(), corescope.User("user-1"), "a1"); !errors.Is(err, coreerrs.ErrNotFound) {
		t.Fatalf("跨用户错误 = %v, want not_found", err)
	}
}

// Snapshot 透传状态计数；作用域原样传给仓储。
func TestSnapshotPassesScopeAndCounts(t *testing.T) {
	st := &fakeStore{counts: map[string]int{string(corestatus.TaskPending): 2, string(corestatus.TaskSuccess): 5}}
	svc := newDiagService(t, &fakeAccounts{}, st)

	view, err := svc.Snapshot(context.Background(), corescope.Master())
	if err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	if view.States[string(corestatus.TaskPending)] != 2 || view.States[string(corestatus.TaskSuccess)] != 5 {
		t.Fatalf("states = %v", view.States)
	}
	if view.Day != day {
		t.Fatalf("day = %q, want %q", view.Day, day)
	}
	if st.gotScope.Kind() != corescope.KindMaster {
		t.Fatalf("scope kind = %v", st.gotScope.Kind())
	}
}

// Snapshot 必须把非 Master 作用域原样下传。
// 判据：admin 作用域进、admin 作用域出。
// 把作用域替换为 Master（或忽略入参）时本用例红。
func TestSnapshotForwardsNonMasterScope(t *testing.T) {
	st := &fakeStore{counts: map[string]int{string(corestatus.TaskSuccess): 1}}
	svc := newDiagService(t, &fakeAccounts{}, st)

	schools := []string{"school-1"}
	view, err := svc.Snapshot(context.Background(), corescope.Admin(schools))
	if err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	if view.States[string(corestatus.TaskSuccess)] != 1 {
		t.Fatalf("states = %v", view.States)
	}
	if kind := st.gotScope.Kind(); kind != corescope.KindAdmin {
		t.Fatalf("scope kind = %v; want admin (the use case replaced the caller scope)", kind)
	}
	if got := st.gotScope.SchoolIDs(); !reflect.DeepEqual(got, schools) {
		t.Fatalf("school ids = %v; want %v", got, schools)
	}
}

// Snapshot 拒绝零值作用域：查库之前报 internal_error。
// 删掉 RequireScope 时零值会被下传——本用例红。
func TestSnapshotRejectsZeroValueScope(t *testing.T) {
	st := &fakeStore{counts: map[string]int{string(corestatus.TaskSuccess): 1}}
	svc := newDiagService(t, &fakeAccounts{}, st)

	if _, err := svc.Snapshot(context.Background(), corescope.Scope{}); err == nil {
		t.Fatal("Snapshot accepted a zero-value scope")
	} else if code := coreerrs.From(err).Code; code != coreerrs.CodeInternal {
		t.Fatalf("zero-value scope maps to %q; want internal_error", code)
	}
	if st.stateCalls != 0 {
		t.Fatalf("StateCounts was called %d times with a zero-value scope", st.stateCalls)
	}
}
