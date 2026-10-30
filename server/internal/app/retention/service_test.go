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
	sessions, resets, tasks [][]int64
	errOn                   string

	cutoffs []string
	calls   int
}

func (f *fakeStore) next(name string, batches *[][]int64) (int64, error) {
	f.calls++
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
		sessions: [][]int64{{appretention.BatchSize}, {500}},
		resets:   [][]int64{{3}},
		tasks:    [][]int64{{appretention.BatchSize}, {0}},
	}
	pruned, err := newService(t, st).Prune(context.Background())
	if err != nil {
		t.Fatalf("Prune: %v", err)
	}
	want := appretention.Pruned{Sessions: appretention.BatchSize + 500, ResetTokens: 3, Tasks: appretention.BatchSize}
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
