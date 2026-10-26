package egress_test

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	appegress "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/egress"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

// fakeStore 模拟粘性绑定：首次写入后不再覆盖（与库主键语义一致）。
type fakeStore struct {
	mu       sync.Mutex
	enabled  []appegress.Egress
	assigned map[string]string
}

func newFakeStore(ids ...string) *fakeStore {
	f := &fakeStore{assigned: map[string]string{}}
	for _, id := range ids {
		f.enabled = append(f.enabled, appegress.Egress{ID: id, Identity: "https://" + id})
	}
	return f
}

func (f *fakeStore) ListEnabled(context.Context) ([]appegress.Egress, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]appegress.Egress(nil), f.enabled...), nil
}

func (f *fakeStore) GetAssignment(_ context.Context, accountID string) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	id, ok := f.assigned[accountID]
	if !ok {
		return "", coreerrs.ErrNotFound
	}
	return id, nil
}

func (f *fakeStore) AssignOrGet(_ context.Context, accountID, preferred string) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if id, ok := f.assigned[accountID]; ok {
		return id, nil
	}
	f.assigned[accountID] = preferred
	return preferred, nil
}

// testNow 是固定时刻：健康冷却判定不依赖真实墙钟。
var testNow = time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)

func newService(t *testing.T, st *fakeStore) *appegress.Service {
	t.Helper()
	svc, err := appegress.NewService(appegress.Deps{Store: st, Clock: coreclock.Fixed{T: testNow}})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

// 粘性：同一账号两次分配得同一出口；出口清单变化也不迁移。
func TestAssignIsSticky(t *testing.T) {
	st := newFakeStore("e1", "e2")
	svc := newService(t, st)
	a, err := svc.Assign(context.Background(), "acct-1")
	if err != nil {
		t.Fatalf("assign: %v", err)
	}
	b, err := svc.Assign(context.Background(), "acct-1")
	if err != nil {
		t.Fatalf("reassign: %v", err)
	}
	if a.ID != b.ID {
		t.Fatalf("二次分配漂移: %s vs %s", a.ID, b.ID)
	}
	// 出口清单变化（新增 e3）：既有绑定不动。
	st.mu.Lock()
	st.enabled = append(st.enabled, appegress.Egress{ID: "e3", Identity: "https://e3"})
	st.mu.Unlock()
	c, err := svc.Assign(context.Background(), "acct-1")
	if err != nil {
		t.Fatalf("assign after change: %v", err)
	}
	if c.ID != a.ID {
		t.Fatalf("清单变化后绑定被迁移: %s -> %s", a.ID, c.ID)
	}
}

// 确定性：同一账号在不同服务实例上首选同一出口。
func TestPickDeterministic(t *testing.T) {
	st := newFakeStore("e1", "e2", "e3")
	first, err := newService(t, st).Assign(context.Background(), "acct-42")
	if err != nil {
		t.Fatalf("assign: %v", err)
	}
	st2 := newFakeStore("e1", "e2", "e3")
	second, err := newService(t, st2).Assign(context.Background(), "acct-42")
	if err != nil {
		t.Fatalf("assign2: %v", err)
	}
	if first.ID != second.ID {
		t.Fatalf("确定性破坏: %s vs %s", first.ID, second.ID)
	}
}

// 分布：多账号应覆盖多个出口（不塌缩到单出口）。
func TestAssignmentSpreads(t *testing.T) {
	st := newFakeStore("e1", "e2")
	svc := newService(t, st)
	seen := map[string]bool{}
	for i := 0; i < 20; i++ {
		e, err := svc.Assign(context.Background(), "acct-"+string(rune('a'+i)))
		if err != nil {
			t.Fatalf("assign %d: %v", i, err)
		}
		seen[e.ID] = true
	}
	if len(seen) != 2 {
		t.Fatalf("20 个账号只用了 %d 个出口: %v", len(seen), seen)
	}
}

// 故障隔离：新账号避开不健康出口；已绑定账号不被迁移。
func TestUnhealthySkippedForNewOnly(t *testing.T) {
	st := newFakeStore("e1", "e2")
	svc := newService(t, st)
	// 先绑定一个账号到 e1 或 e2，再把两个出口都标记不健康：
	a, err := svc.Assign(context.Background(), "acct-old")
	if err != nil {
		t.Fatalf("assign: %v", err)
	}
	svc.MarkFailed("e1", testNow.Add(time.Hour))
	svc.MarkFailed("e2", testNow.Add(time.Hour))
	// 已绑定账号：仍返回原出口（隔离而非漂移）。
	b, err := svc.Assign(context.Background(), "acct-old")
	if err != nil {
		t.Fatalf("assign bound: %v", err)
	}
	if b.ID != a.ID {
		t.Fatalf("故障导致迁移: %s -> %s", a.ID, b.ID)
	}
	// 新账号：全不健康时退化到全部启用出口（宁可慢，不判无出口）。
	if _, err := svc.Assign(context.Background(), "acct-new"); err != nil {
		t.Fatalf("全不健康不应拒绝: %v", err)
	}

	// 只标记一个出口不健康：新账号必须全部避开它。
	st2 := newFakeStore("e1", "e2")
	svc2 := newService(t, st2)
	svc2.MarkFailed("e1", testNow.Add(time.Hour))
	for i := 0; i < 20; i++ {
		got, err := svc2.Assign(context.Background(), "fresh-"+string(rune('a'+i)))
		if err != nil {
			t.Fatalf("assign fresh %d: %v", i, err)
		}
		if got.ID == "e1" {
			t.Fatalf("新账号选中了不健康出口: %s", got.ID)
		}
	}
}

// 无可选出口：响亮拒绝。
func TestNoEnabledEgress(t *testing.T) {
	svc := newService(t, newFakeStore())
	if _, err := svc.Assign(context.Background(), "acct-1"); err == nil {
		t.Fatal("无启用出口应报错")
	}
}

// 空账号：校验失败。
func TestEmptyAccountRejected(t *testing.T) {
	svc := newService(t, newFakeStore("e1"))
	if _, err := svc.Assign(context.Background(), ""); err == nil {
		t.Fatal("空账号应报错")
	}
}

// 并发首次分配：单绑定收敛（后到者读先到者的值）。
func TestConcurrentAssignSingleBinding(t *testing.T) {
	st := newFakeStore("e1", "e2", "e3")
	svc := newService(t, st)
	var wg sync.WaitGroup
	results := make(chan string, 16)
	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			e, err := svc.Assign(context.Background(), "acct-race")
			if err != nil {
				t.Errorf("assign: %v", err)
				return
			}
			results <- e.ID
		}()
	}
	wg.Wait()
	close(results)
	var first string
	for id := range results {
		if first == "" {
			first = id
			continue
		}
		if id != first {
			t.Fatalf("并发分配出两个绑定: %s vs %s", first, id)
		}
	}
}

// 读错误（非 NotFound）必须上抛。
func TestReadErrorPropagates(t *testing.T) {
	st := &errStore{err: errors.New("db down")}
	svc, err := appegress.NewService(appegress.Deps{Store: st, Clock: coreclock.Fixed{T: testNow}})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	if _, err := svc.Assign(context.Background(), "acct-1"); err == nil {
		t.Fatal("存储错误应上抛")
	}
}

type errStore struct{ err error }

func (e *errStore) ListEnabled(context.Context) ([]appegress.Egress, error) { return nil, e.err }
func (e *errStore) GetAssignment(context.Context, string) (string, error)   { return "", e.err }
func (e *errStore) AssignOrGet(context.Context, string, string) (string, error) {
	return "", e.err
}
