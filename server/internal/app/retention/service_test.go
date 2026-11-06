package retention_test

// 清理用例的记账判据：分批跑到不足一批、保留期按业务时区算、单表失败即止。

import (
	"context"
	"errors"
	"testing"
	"time"

	appretention "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/retention"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
)

// fakeStore 按预设批次返回行数；批次用完后恒返回 0。
type fakeStore struct {
	sessions, resets, tasks, notifications, accounts, users [][]int64
	errOn                                                   string

	cutoffs             []string
	notificationCutoffs []time.Time
	accountCutoffs      []time.Time
	userCutoffs         []time.Time
	order               []string
}

func (f *fakeStore) next(name string, batches *[][]int64) (int64, error) {
	f.order = append(f.order, name)
	if f.errOn == name {
		return 0, errors.New("db down")
	}
	if len(*batches) == 0 {
		return 0, nil
	}
	n := (*batches)[0][0]
	*batches = (*batches)[1:]
	return n, nil
}

func (f *fakeStore) DeleteExpiredSessions(context.Context, time.Time, int) (int64, error) {
	return f.next("sessions", &f.sessions)
}

func (f *fakeStore) DeleteExpiredPasswordResets(context.Context, time.Time, int) (int64, error) {
	return f.next("resets", &f.resets)
}

func (f *fakeStore) DeleteTasksBefore(_ context.Context, before string, _ int) (int64, error) {
	f.cutoffs = append(f.cutoffs, before)
	return f.next("tasks", &f.tasks)
}

func (f *fakeStore) DeleteNotificationsBefore(_ context.Context, before time.Time, _ int) (int64, error) {
	f.notificationCutoffs = append(f.notificationCutoffs, before)
	return f.next("notifications", &f.notifications)
}

func (f *fakeStore) DeleteAccountsDeletedBefore(_ context.Context, before time.Time, _ int) (int64, error) {
	f.accountCutoffs = append(f.accountCutoffs, before)
	return f.next("accounts", &f.accounts)
}

func (f *fakeStore) DeleteUsersDeletedBefore(_ context.Context, before time.Time, _ int) (int64, error) {
	f.userCutoffs = append(f.userCutoffs, before)
	return f.next("users", &f.users)
}

var shanghai = time.FixedZone("CST", 8*3600)

func newService(t *testing.T, st *fakeStore) *appretention.Service {
	t.Helper()
	now := time.Date(2026, 10, 31, 9, 0, 0, 0, shanghai)
	svc, err := appretention.NewService(appretention.Deps{
		Store: st, Clock: coreclock.Fixed{T: now}, Location: shanghai,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

// 每张表跑到不足一批为止，总数按批累加。
func TestPruneDrainsEachTable(t *testing.T) {
	st := &fakeStore{
		sessions:      [][]int64{{appretention.BatchSize}, {500}},
		resets:        [][]int64{{3}},
		tasks:         [][]int64{{appretention.BatchSize}, {0}},
		notifications: [][]int64{{4}},
		accounts:      [][]int64{{appretention.BatchSize}, {7}},
		users:         [][]int64{{2}},
	}
	pruned, err := newService(t, st).Prune(context.Background())
	if err != nil {
		t.Fatalf("Prune: %v", err)
	}
	want := appretention.Pruned{
		Sessions:      appretention.BatchSize + 500,
		ResetTokens:   3,
		Tasks:         appretention.BatchSize,
		Notifications: 4,
		Accounts:      appretention.BatchSize + 7,
		Users:         2,
	}
	if pruned != want {
		t.Fatalf("pruned = %+v, want %+v", pruned, want)
	}
}

// 任务行保留期按 Location 算：2026-10-31 减 90 天 = 2026-08-02。
func TestPruneTaskCutoffUsesLocation(t *testing.T) {
	st := &fakeStore{}
	if _, err := newService(t, st).Prune(context.Background()); err != nil {
		t.Fatalf("Prune: %v", err)
	}
	if len(st.cutoffs) != 1 || st.cutoffs[0] != "2026-08-02" {
		t.Fatalf("cutoff = %v, want 2026-08-02", st.cutoffs)
	}
}

// 站内信保留期是 30 天，按同一时刻回退：
// 2026-10-31 09:00 +08:00 减 30 天 = 2026-10-01 09:00 +08:00。
func TestPruneNotificationCutoffIsThirtyDays(t *testing.T) {
	st := &fakeStore{}
	if _, err := newService(t, st).Prune(context.Background()); err != nil {
		t.Fatalf("Prune: %v", err)
	}
	want := time.Date(2026, 10, 1, 9, 0, 0, 0, shanghai)
	if len(st.notificationCutoffs) != 1 || !st.notificationCutoffs[0].Equal(want) {
		t.Fatalf("notification cutoff = %v, want %v", st.notificationCutoffs, want)
	}
}

// 软删账号保留期是 30 天，按同一时刻回退：
// 2026-10-31 09:00 +08:00 减 30 天 = 2026-10-01 09:00 +08:00。
func TestPruneAccountCutoffIsThirtyDays(t *testing.T) {
	st := &fakeStore{}
	if _, err := newService(t, st).Prune(context.Background()); err != nil {
		t.Fatalf("Prune: %v", err)
	}
	want := time.Date(2026, 10, 1, 9, 0, 0, 0, shanghai)
	if len(st.accountCutoffs) != 1 || !st.accountCutoffs[0].Equal(want) {
		t.Fatalf("account cutoff = %v, want %v", st.accountCutoffs, want)
	}
}

// 软删用户保留期是 90 天：
// 2026-10-31 09:00 +08:00 减 90 天 = 2026-08-02 09:00 +08:00。
func TestPruneUserCutoffIsNinetyDays(t *testing.T) {
	st := &fakeStore{}
	if _, err := newService(t, st).Prune(context.Background()); err != nil {
		t.Fatalf("Prune: %v", err)
	}
	want := time.Date(2026, 8, 2, 9, 0, 0, 0, shanghai)
	if len(st.userCutoffs) != 1 || !st.userCutoffs[0].Equal(want) {
		t.Fatalf("user cutoff = %v, want %v", st.userCutoffs, want)
	}
}

// 表间顺序钉死：账号先于用户（accounts.owner_user_id 是 RESTRICT），
// 六张表依次各跑一轮。
func TestPruneRunsAccountsBeforeUsers(t *testing.T) {
	st := &fakeStore{}
	if _, err := newService(t, st).Prune(context.Background()); err != nil {
		t.Fatalf("Prune: %v", err)
	}
	want := []string{"sessions", "resets", "tasks", "notifications", "accounts", "users"}
	if len(st.order) != len(want) {
		t.Fatalf("call order = %v, want %v", st.order, want)
	}
	for i, name := range want {
		if st.order[i] != name {
			t.Fatalf("call order = %v, want %v", st.order, want)
		}
	}
}

// 批量上限：满批就继续循环，不足一批才停。
// 账号表三批 1000/1000/5：删 2005 行，账号表共调 3 次。
func TestPruneAccountsDrainLoopsUntilShortBatch(t *testing.T) {
	st := &fakeStore{
		accounts: [][]int64{{appretention.BatchSize}, {appretention.BatchSize}, {5}},
	}
	pruned, err := newService(t, st).Prune(context.Background())
	if err != nil {
		t.Fatalf("Prune: %v", err)
	}
	if pruned.Accounts != 2*appretention.BatchSize+5 {
		t.Fatalf("accounts = %d, want %d", pruned.Accounts, 2*appretention.BatchSize+5)
	}
	accountCalls := 0
	for _, name := range st.order {
		if name == "accounts" {
			accountCalls++
		}
	}
	if accountCalls != 3 {
		t.Fatalf("账号表调用 %d 次, want 3（两次满批加一次收尾）", accountCalls)
	}
}

// 单表失败即止：不继续动后面的表，返回已删数。
func TestPruneStopsOnError(t *testing.T) {
	st := &fakeStore{
		sessions: [][]int64{{7}},
		errOn:    "resets",
	}
	pruned, err := newService(t, st).Prune(context.Background())
	if err == nil {
		t.Fatal("Prune 应返回错误")
	}
	if pruned.Sessions != 7 || pruned.Tasks != 0 {
		t.Fatalf("pruned = %+v, want sessions=7 tasks=0", pruned)
	}
	if len(st.cutoffs) != 0 {
		t.Fatalf("失败后不该再动任务表, cutoff = %v", st.cutoffs)
	}
}

// 用户清理失败：返回错误与已删数；用户表只调一次（失败的那次），不做循环。
func TestPruneStopsOnErrorOnUsers(t *testing.T) {
	st := &fakeStore{
		accounts: [][]int64{{11}},
		errOn:    "users",
	}
	pruned, err := newService(t, st).Prune(context.Background())
	if err == nil {
		t.Fatal("Prune 应返回错误")
	}
	if pruned.Accounts != 11 || pruned.Users != 0 {
		t.Fatalf("pruned = %+v, want accounts=11 users=0", pruned)
	}
	if len(st.userCutoffs) != 1 {
		t.Fatalf("用户表应只调一次失败调用, got %d", len(st.userCutoffs))
	}
}

// 账号清理失败：用户表不再调用，返回错误。
func TestPruneStopsOnErrorOnAccounts(t *testing.T) {
	st := &fakeStore{
		accounts: [][]int64{{11}},
		errOn:    "accounts",
	}
	pruned, err := newService(t, st).Prune(context.Background())
	if err == nil {
		t.Fatal("Prune 应返回错误")
	}
	if pruned.Accounts != 0 || pruned.Users != 0 {
		t.Fatalf("pruned = %+v, want accounts=0 users=0", pruned)
	}
	if len(st.userCutoffs) != 0 {
		t.Fatalf("失败后不该再动用户表")
	}
}
