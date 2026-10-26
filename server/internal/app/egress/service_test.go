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
	// all 是"已停用但仍存在"的出口（Get 能取到，ListEnabled 取不到）。
	all map[string]appegress.Egress
}

func newFakeStore(ids ...string) *fakeStore {
	f := &fakeStore{assigned: map[string]string{}, all: map[string]appegress.Egress{}}
	for _, id := range ids {
		f.enabled = append(f.enabled, appegress.Egress{ID: id, Identity: "https://" + id})
	}
	return f
}

func (f *fakeStore) Get(_ context.Context, egressID string) (appegress.Egress, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, e := range f.enabled {
		if e.ID == egressID {
			return e, nil
		}
	}
	if id, ok := f.all[egressID]; ok {
		return id, nil
	}
	return appegress.Egress{}, coreerrs.ErrNotFound
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
	if a.Identity != "https://"+a.ID {
		t.Fatalf("首次分配 Identity 失真: %+v", a)
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

// 无可选出口：响亮拒绝，且错误码是 capacity_exceeded（运维配置面）。
func TestNoEnabledEgress(t *testing.T) {
	svc := newService(t, newFakeStore())
	_, err := svc.Assign(context.Background(), "acct-1")
	if err == nil {
		t.Fatal("无启用出口应报错")
	}
	if got := coreerrs.From(err).Code; got != coreerrs.CodeCapacity {
		t.Fatalf("错误码 = %s, want capacity_exceeded", got)
	}
}

// 绑定指向不存在的出口：属数据异常，必须响亮失败（不得返回空记录）。
func TestBoundMissingEgressFails(t *testing.T) {
	st := newFakeStore("e1")
	st.assigned["acct-gone"] = "missing-id"
	svc := newService(t, st)
	if _, err := svc.Assign(context.Background(), "acct-gone"); err == nil {
		t.Fatal("绑定指向不存在出口应报错")
	}
}

// ListEnabled 错误必须上抛（不得当空清单吞掉）。
func TestListEnabledErrorPropagates(t *testing.T) {
	st := &listErrStore{}
	svc, err := appegress.NewService(appegress.Deps{Store: st, Clock: coreclock.Fixed{T: testNow}})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	if _, err := svc.Assign(context.Background(), "acct-1"); err == nil {
		t.Fatal("ListEnabled 错误应上抛")
	}
}

type listErrStore struct{}

func (s *listErrStore) Get(context.Context, string) (appegress.Egress, error) {
	return appegress.Egress{}, coreerrs.ErrNotFound
}
func (s *listErrStore) ListEnabled(context.Context) ([]appegress.Egress, error) {
	return nil, errors.New("list failed")
}
func (s *listErrStore) GetAssignment(context.Context, string) (string, error) {
	return "", coreerrs.ErrNotFound
}
func (s *listErrStore) AssignOrGet(context.Context, string, string) (string, error) {
	return "", nil
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

func (e *errStore) Get(context.Context, string) (appegress.Egress, error) {
	return appegress.Egress{}, e.err
}
func (e *errStore) ListEnabled(context.Context) ([]appegress.Egress, error) { return nil, e.err }
func (e *errStore) GetAssignment(context.Context, string) (string, error)   { return "", e.err }
func (e *errStore) AssignOrGet(context.Context, string, string) (string, error) {
	return "", e.err
}

// 停用绑定的 Identity 必须仍可取：粘性不因运维停用而失效（复审 P2-1）。
func TestBoundDisabledEgressKeepsIdentity(t *testing.T) {
	st := newFakeStore("e1", "e2")
	svc := newService(t, st)
	a, err := svc.Assign(context.Background(), "acct-d")
	if err != nil {
		t.Fatalf("assign: %v", err)
	}
	// 停用该出口：从 enabled 移走，但仍在 all 里（库行未删）。
	st.mu.Lock()
	kept := appegress.Egress{ID: a.ID, Identity: a.Identity}
	st.all[a.ID] = kept
	var rest []appegress.Egress
	for _, e := range st.enabled {
		if e.ID != a.ID {
			rest = append(rest, e)
		}
	}
	st.enabled = rest
	st.mu.Unlock()

	b, err := svc.Assign(context.Background(), "acct-d")
	if err != nil {
		t.Fatalf("assign bound disabled: %v", err)
	}
	if b.ID != a.ID || b.Identity != a.Identity {
		t.Fatalf("停用后绑定失真: %+v, want %+v", b, kept)
	}
	// 全部停用时，已绑定账号仍可用（绑定优先于启用清单）。
	st.mu.Lock()
	st.enabled = nil
	st.mu.Unlock()
	c, err := svc.Assign(context.Background(), "acct-d")
	if err != nil || c.Identity != a.Identity {
		t.Fatalf("全停用时绑定应可用: %+v, %v", c, err)
	}
}

// GetAssignment 真错误不得落穿成"新分配"（复审 P3-4）：
// ListEnabled 正常、GetAssignment 报普通错误 → 必须上抛。
func TestAssignmentReadErrorDoesNotFallThrough(t *testing.T) {
	st := &assignErrStore{enabled: []appegress.Egress{{ID: "e1", Identity: "https://e1"}}}
	svc, err := appegress.NewService(appegress.Deps{Store: st, Clock: coreclock.Fixed{T: testNow}})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	if _, err := svc.Assign(context.Background(), "acct-1"); err == nil {
		t.Fatal("GetAssignment 真错误应上抛，不得继续分配")
	}
}

// AssignOrGet 失败必须上抛。
func TestAssignWriteErrorPropagates(t *testing.T) {
	st := &writeErrStore{enabled: []appegress.Egress{{ID: "e1", Identity: "https://e1"}}}
	svc, err := appegress.NewService(appegress.Deps{Store: st, Clock: coreclock.Fixed{T: testNow}})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	if _, err := svc.Assign(context.Background(), "acct-1"); err == nil {
		t.Fatal("AssignOrGet 失败应上抛")
	}
}

type assignErrStore struct{ enabled []appegress.Egress }

func (s *assignErrStore) Get(context.Context, string) (appegress.Egress, error) {
	return appegress.Egress{}, nil
}
func (s *assignErrStore) ListEnabled(context.Context) ([]appegress.Egress, error) {
	return s.enabled, nil
}
func (s *assignErrStore) GetAssignment(context.Context, string) (string, error) {
	return "", errors.New("read failed")
}
func (s *assignErrStore) AssignOrGet(context.Context, string, string) (string, error) {
	return "", nil
}

type writeErrStore struct{ enabled []appegress.Egress }

func (s *writeErrStore) Get(context.Context, string) (appegress.Egress, error) {
	return appegress.Egress{}, coreerrs.ErrNotFound
}
func (s *writeErrStore) ListEnabled(context.Context) ([]appegress.Egress, error) {
	return s.enabled, nil
}
func (s *writeErrStore) GetAssignment(context.Context, string) (string, error) {
	return "", coreerrs.ErrNotFound
}
func (s *writeErrStore) AssignOrGet(context.Context, string, string) (string, error) {
	return "", errors.New("write failed")
}
