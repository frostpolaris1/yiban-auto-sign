package schedule_test

import (
	"context"
	"errors"
	"reflect"
	"testing"
	"time"

	appschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schedule"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// ---- 测试替身 ----
// 模拟器在"窗口解析与落点分布"这一维等价：状态机真实、无 I/O。
// 读缺失用 errs.ErrNotFound（与 pg 的映射一致）；其他错误按真错误上抛。

type fakeWindowStore struct {
	record coreschedule.WindowRecord
	ok     bool
	// readErr 非 nil 时读返回该错误（模拟连接失败）。
	readErr error
	// staleOnce 为真时第一次写之前先由"并发写者"抬版本。
	staleOnce bool
	staled    bool
	saves     int
	lastExp   int
}

func (f *fakeWindowStore) GetWindow(_ context.Context, taskID, day string) (coreschedule.WindowRecord, error) {
	if f.readErr != nil {
		return coreschedule.WindowRecord{}, f.readErr
	}
	if !f.ok {
		return coreschedule.WindowRecord{}, coreerrs.ErrNotFound
	}
	f.record.SchoolTaskID, f.record.Day = taskID, day
	return f.record, nil
}

func (f *fakeWindowStore) PutWindow(_ context.Context, w coreschedule.WindowRecord, expectedVersion int) (int, error) {
	f.lastExp = expectedVersion
	if f.staleOnce && !f.staled {
		// 并发写者先进：版本 +1，本次写入必然基于旧版本。
		f.record.Version++
		f.staled = true
	}
	if expectedVersion > 0 && expectedVersion != f.record.Version {
		return 0, coreschedule.ErrWindowStale
	}
	version := f.record.Version + 1
	w.Version = version
	f.record, f.ok = w, true
	f.saves++
	return version, nil
}

type fakePlanStore struct {
	inserted   [][]coreschedule.PlanRow
	rearranged [][]coreschedule.PlanRow
	existing   map[string]bool
}

func (f *fakePlanStore) InsertPlan(_ context.Context, rows []coreschedule.PlanRow) (int64, error) {
	f.inserted = append(f.inserted, rows)
	if f.existing == nil {
		f.existing = map[string]bool{}
	}
	var n int64
	for _, r := range rows {
		key := r.AccountID + "|" + r.SchoolTaskID + "|" + r.Day
		if f.existing[key] {
			continue
		}
		f.existing[key] = true
		n++
	}
	return n, nil
}

func (f *fakePlanStore) RearrangePlan(_ context.Context, taskID, day string, rows []coreschedule.PlanRow) (int64, error) {
	f.rearranged = append(f.rearranged, rows)
	return int64(len(rows)), nil
}

type fakeAccounts struct{ ids []string }

func (f fakeAccounts) ActiveAccountIDs(context.Context, string) ([]string, error) {
	return f.ids, nil
}

type fakeDiscover struct {
	window contract.Window
	err    error
	calls  int
}

func (f *fakeDiscover) DiscoverWindow(context.Context, string) (contract.Window, error) {
	f.calls++
	return f.window, f.err
}

// fakeTx 直接执行：事务原子的等价物在 pg；此处的判据是"三步都在同一闭包内"。
type fakeTx struct{ calls int }

func (f *fakeTx) InTx(ctx context.Context, fn func(context.Context) error) error {
	f.calls++
	return fn(ctx)
}

// ---- 公共输入 ----

var shanghai = time.FixedZone("CST", 8*3600)

func baseInput() appschedule.Input {
	return appschedule.Input{
		SchoolID: "school-1", SchoolTaskID: "task-1", Day: "2026-10-29",
		Location: shanghai,
		Workers:  6, Avg: 3 * time.Second, Rate: 1.0, Util: 0.8,
		SlotsPerSlice: 10, Front: true,
		EdgeFront: 0, EdgeBack: 0,
	}
}

func platformWindow(h, m, eh, em int) contract.Window {
	return contract.Window{
		Start:  time.Date(2026, 10, 29, h, m, 0, 0, shanghai),
		End:    time.Date(2026, 10, 29, eh, em, 0, 0, shanghai),
		Source: contract.WindowPlatform,
	}
}

func newService(t *testing.T, w *fakeWindowStore, p *fakePlanStore, a fakeAccounts, d *fakeDiscover) *appschedule.Service {
	t.Helper()
	svc, err := appschedule.NewService(appschedule.Deps{
		Windows: w, Plans: p, Accounts: a, Discover: d, Tx: &fakeTx{},
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

// 预览就是计划：行、直方图、落库行三者逐值相等（复审 P2-2 的钉子）。
func TestPreviewEqualsBuild(t *testing.T) {
	w := &fakeWindowStore{}
	p := &fakePlanStore{}
	ids := make([]string, 25)
	for i := range ids {
		ids[i] = "acct-" + string(rune('a'+i%26)) + string(rune('0'+i/26))
	}
	accts := fakeAccounts{ids: ids}
	d := &fakeDiscover{window: platformWindow(6, 30, 7, 50)}
	svc := newService(t, w, p, accts, d)
	in := baseInput()

	prev, err := svc.Preview(context.Background(), in)
	if err != nil {
		t.Fatalf("Preview: %v", err)
	}
	built, err := svc.Build(context.Background(), in)
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	if !reflect.DeepEqual(prev.Rows, built.Rows) {
		t.Fatalf("行不同源:\npreview=%v\nbuild=%v", prev.Rows, built.Rows)
	}
	if !reflect.DeepEqual(prev.Histogram, built.Histogram) {
		t.Fatalf("直方图不同源: %v vs %v", prev.Histogram, built.Histogram)
	}
	if len(p.inserted) != 1 || len(p.inserted[0]) != 25 {
		t.Fatalf("落库行数 = %+v, want 25", p.inserted)
	}
	// 直方图必须与行的真实落点一致（25 行 / 10 槽/片 → 跨 3 片）。
	if len(prev.Histogram) < 3 {
		t.Fatalf("直方图片数 = %d, want >=3: %v", len(prev.Histogram), prev.Histogram)
	}
	sum, want := 0, map[int]int{}
	for _, r := range prev.Rows {
		want[int(r.RunAt.Sub(prev.WindowStart)/(5*time.Minute))]++
	}
	for k, v := range prev.Histogram {
		sum += v
		if want[k] != v {
			t.Fatalf("片 %d 直方图 = %d, 行落点 = %d", k, v, want[k])
		}
	}
	if sum != 25 {
		t.Fatalf("直方图和 = %d, want 25", sum)
	}
	// 落库行的账号与时刻必须逐值等于预览行（防 planRows 偏移）。
	for i, row := range p.inserted[0] {
		if row.AccountID != prev.Rows[i].AccountID || !row.RunAt.Equal(prev.Rows[i].RunAt) {
			t.Fatalf("落库行 %d 与预览不一致: %+v vs %+v", i, row, prev.Rows[i])
		}
	}
	if w.saves != 1 || len(p.rearranged) != 1 {
		t.Fatalf("首次构建 saves=%d rearranged=%d, want 1/1", w.saves, len(p.rearranged))
	}
}

// 两校不同窗口互不串窗：各自落点只在自己的窗口内。
func TestTwoSchoolsDoNotCross(t *testing.T) {
	w := &fakeWindowStore{}
	p := &fakePlanStore{}
	accts := fakeAccounts{ids: []string{"a1", "a2", "a3"}}
	d := &fakeDiscover{window: platformWindow(6, 30, 7, 50)}
	svc := newService(t, w, p, accts, d)

	prevA, err := svc.Preview(context.Background(), baseInput())
	if err != nil {
		t.Fatalf("preview A: %v", err)
	}
	inB := baseInput()
	inB.SchoolTaskID = "task-2"
	d.window = platformWindow(20, 0, 21, 0)
	prevB, err := svc.Preview(context.Background(), inB)
	if err != nil {
		t.Fatalf("preview B: %v", err)
	}
	for _, r := range prevA.Rows {
		if r.RunAt.Before(prevA.WindowStart) || !r.RunAt.Before(prevA.WindowEnd) {
			t.Fatalf("甲校行越窗: %v", r.RunAt)
		}
	}
	for _, r := range prevB.Rows {
		if r.RunAt.Before(prevB.WindowStart) || !r.RunAt.Before(prevB.WindowEnd) {
			t.Fatalf("乙校行越窗: %v", r.RunAt)
		}
	}
	if !prevB.WindowStart.After(prevA.WindowEnd) {
		t.Fatalf("两窗重叠: A=[%v,%v] B=[%v,%v]", prevA.WindowStart, prevA.WindowEnd, prevB.WindowStart, prevB.WindowEnd)
	}
}

// 窗口来源优先级：平台 > 缓存 > 回退。
func TestWindowSourcePrecedence(t *testing.T) {
	cached := coreschedule.WindowRecord{
		Start:  time.Date(2026, 10, 29, 7, 0, 0, 0, shanghai),
		End:    time.Date(2026, 10, 29, 8, 0, 0, 0, shanghai),
		Source: string(contract.WindowCache), Version: 3,
	}
	w1 := &fakeWindowStore{record: cached, ok: true}
	d1 := &fakeDiscover{window: platformWindow(6, 30, 7, 50)}
	svc := newService(t, w1, &fakePlanStore{}, fakeAccounts{ids: []string{"a1"}}, d1)
	prev, err := svc.Preview(context.Background(), baseInput())
	if err != nil {
		t.Fatalf("preview: %v", err)
	}
	if prev.Source != contract.WindowPlatform {
		t.Fatalf("来源 = %s, want platform", prev.Source)
	}
	w2 := &fakeWindowStore{record: cached, ok: true}
	d2 := &fakeDiscover{err: errors.New("upstream down")}
	svc = newService(t, w2, &fakePlanStore{}, fakeAccounts{ids: []string{"a1"}}, d2)
	prev, err = svc.Preview(context.Background(), baseInput())
	if err != nil {
		t.Fatalf("preview cache: %v", err)
	}
	if prev.Source != contract.WindowCache {
		t.Fatalf("来源 = %s, want cache", prev.Source)
	}
	d3 := &fakeDiscover{err: errors.New("upstream down")}
	svc = newService(t, &fakeWindowStore{}, &fakePlanStore{}, fakeAccounts{ids: []string{"a1"}}, d3)
	prev, err = svc.Preview(context.Background(), baseInput())
	if err != nil {
		t.Fatalf("preview fallback: %v", err)
	}
	if prev.Source != contract.WindowFallback {
		t.Fatalf("来源 = %s, want fallback", prev.Source)
	}
	if !prev.WindowStart.Equal(time.Date(2026, 10, 29, 6, 30, 0, 0, shanghai)) ||
		!prev.WindowEnd.Equal(time.Date(2026, 10, 29, 7, 50, 0, 0, shanghai)) {
		t.Fatalf("回退窗口 = [%v, %v], want 06:30~07:50", prev.WindowStart, prev.WindowEnd)
	}
}

// 容量缺口如实上报；超容量的行仍在窗内（顺延不丢号）。
func TestCapacityShortfallReported(t *testing.T) {
	ids := make([]string, 100)
	for i := range ids {
		ids[i] = "acct-" + string(rune('a'+i%26)) + string(rune('0'+i/26))
	}
	svc := newService(t, &fakeWindowStore{}, &fakePlanStore{}, fakeAccounts{ids: ids},
		&fakeDiscover{window: platformWindow(6, 30, 7, 50)})
	in := baseInput()
	in.Workers = 1
	in.Avg = 60 * time.Second // 吞吐 1/60 次/秒：80 分钟窗口容量 64 < 需求 100
	in.Rate = 1.0
	prev, err := svc.Preview(context.Background(), in)
	if err != nil {
		t.Fatalf("preview: %v", err)
	}
	if prev.Demand != 100 {
		t.Fatalf("Demand = %d, want 100", prev.Demand)
	}
	if prev.Shortfall <= 0 {
		t.Fatalf("应报缺口, capacity=%d demand=%d", prev.Capacity, prev.Demand)
	}
	for _, r := range prev.Rows {
		if r.RunAt.Before(prev.WindowStart) || !r.RunAt.Before(prev.WindowEnd) {
			t.Fatalf("超容量行越窗: %v", r.RunAt)
		}
	}
}

// 窗口**仅起点**变化也必须重排（复审 P2-6：Start 比较的钉子）。
func TestWindowStartOnlyChangeRearranges(t *testing.T) {
	w := &fakeWindowStore{ok: true, record: coreschedule.WindowRecord{
		Start:  time.Date(2026, 10, 29, 6, 30, 0, 0, shanghai),
		End:    time.Date(2026, 10, 29, 7, 50, 0, 0, shanghai),
		Source: string(contract.WindowPlatform), Version: 1,
	}}
	p := &fakePlanStore{}
	d := &fakeDiscover{window: platformWindow(7, 0, 7, 50)} // 只起点变
	svc := newService(t, w, p, fakeAccounts{ids: []string{"a1", "a2"}}, d)
	prev, err := svc.Build(context.Background(), baseInput())
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	if prev.Version != 2 {
		t.Fatalf("版本 = %d, want 2", prev.Version)
	}
	if w.lastExp != 1 {
		t.Fatalf("乐观锁 expected = %d, want 1", w.lastExp)
	}
	if len(p.rearranged) != 1 {
		t.Fatalf("起点变化应重排一次, got %d", len(p.rearranged))
	}
}

// 窗口未变化：不写窗口、不重排，只做幂等补插。
func TestBuildUnchangedSkipsRearrange(t *testing.T) {
	w := &fakeWindowStore{ok: true, record: coreschedule.WindowRecord{
		Start:  time.Date(2026, 10, 29, 6, 30, 0, 0, shanghai),
		End:    time.Date(2026, 10, 29, 7, 50, 0, 0, shanghai),
		Source: string(contract.WindowPlatform), Version: 4,
	}}
	p := &fakePlanStore{}
	d := &fakeDiscover{window: platformWindow(6, 30, 7, 50)}
	svc := newService(t, w, p, fakeAccounts{ids: []string{"a1"}}, d)
	prev, err := svc.Build(context.Background(), baseInput())
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	if prev.Version != 4 {
		t.Fatalf("版本 = %d, want 4（不变）", prev.Version)
	}
	if w.saves != 0 {
		t.Fatalf("窗口未变不应写库, saves=%d", w.saves)
	}
	if len(p.rearranged) != 0 {
		t.Fatalf("窗口未变不应重排, got %d", len(p.rearranged))
	}
	if n := len(p.inserted); n != 1 {
		t.Fatalf("应幂等补插一次, got %d", n)
	}
	if _, err := svc.Build(context.Background(), baseInput()); err != nil {
		t.Fatalf("second build: %v", err)
	}
	if len(p.inserted) != 2 {
		t.Fatalf("第二次应有调用记录, got %d", len(p.inserted))
	}
}

// 亚毫秒窗口：夹取不得产出早于窗口起点的落点（复审 P2-1）。
func TestSubMillisecondWindowClamped(t *testing.T) {
	d := &fakeDiscover{window: contract.Window{
		Start:  time.Date(2026, 10, 29, 6, 30, 0, 0, shanghai),
		End:    time.Date(2026, 10, 29, 6, 30, 0, 500_000, shanghai), // 500µs
		Source: contract.WindowPlatform,
	}}
	svc := newService(t, &fakeWindowStore{}, &fakePlanStore{}, fakeAccounts{ids: []string{"a1", "a2"}}, d)
	in := baseInput()
	in.SlotsPerSlice = 2
	prev, err := svc.Preview(context.Background(), in)
	if err != nil {
		t.Fatalf("preview: %v", err)
	}
	for _, r := range prev.Rows {
		if r.RunAt.Before(prev.WindowStart) || !r.RunAt.Before(prev.WindowEnd) {
			t.Fatalf("亚毫秒窗口越界: %v not in [%v, %v)", r.RunAt, prev.WindowStart, prev.WindowEnd)
		}
	}
}

// 缓存读真错误必须上抛，不得降级为"无缓存"（复审 P2-5）。
func TestCacheReadErrorPropagates(t *testing.T) {
	w := &fakeWindowStore{readErr: errors.New("connection refused")}
	svc := newService(t, w, &fakePlanStore{}, fakeAccounts{ids: []string{"a1"}},
		&fakeDiscover{window: platformWindow(6, 30, 7, 50)})
	if _, err := svc.Preview(context.Background(), baseInput()); err == nil {
		t.Fatal("缓存读真错误应上抛")
	}
}

// 版本竞争：整体重试一次，最终成功且重排一次（复审 P2-4/P3-4）。
func TestStaleRetriesOnce(t *testing.T) {
	w := &fakeWindowStore{ok: true, staleOnce: true, record: coreschedule.WindowRecord{
		Start:  time.Date(2026, 10, 29, 6, 30, 0, 0, shanghai),
		End:    time.Date(2026, 10, 29, 7, 50, 0, 0, shanghai),
		Source: string(contract.WindowPlatform), Version: 1,
	}}
	p := &fakePlanStore{}
	d := &fakeDiscover{window: platformWindow(7, 0, 8, 0)}
	svc := newService(t, w, p, fakeAccounts{ids: []string{"a1"}}, d)
	prev, err := svc.Build(context.Background(), baseInput())
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	if w.saves != 1 {
		t.Fatalf("重试后应成功写一次, saves=%d", w.saves)
	}
	if len(p.rearranged) != 1 {
		t.Fatalf("重排应只发生一次, got %d", len(p.rearranged))
	}
	if prev.Version < 2 {
		t.Fatalf("版本 = %d, want >=2", prev.Version)
	}
}

// 输入校验：非法参数必须响亮拒绝（不静默补缺省）。
func TestInvalidInput(t *testing.T) {
	svc := newService(t, &fakeWindowStore{}, &fakePlanStore{}, fakeAccounts{}, &fakeDiscover{})
	cases := []func(*appschedule.Input){
		func(in *appschedule.Input) { in.SchoolID = "" },
		func(in *appschedule.Input) { in.SchoolTaskID = "" },
		func(in *appschedule.Input) { in.Day = "" },
		func(in *appschedule.Input) { in.Location = nil },
		func(in *appschedule.Input) { in.Workers = 0 },
		func(in *appschedule.Input) { in.Avg = 0 },
		func(in *appschedule.Input) { in.Rate = 0 },
		func(in *appschedule.Input) { in.Util = 0 },
		func(in *appschedule.Input) { in.SlotsPerSlice = 0 },
		func(in *appschedule.Input) { in.Day = "2026/10/29" },
	}
	for i, mutate := range cases {
		in := baseInput()
		mutate(&in)
		if _, err := svc.Preview(context.Background(), in); err == nil {
			t.Fatalf("用例 %d 应报错", i)
		}
	}
}

// 有效窗口裁剪：EdgeFront/EdgeBack 生效。
func TestEdgeTrimApplied(t *testing.T) {
	d := &fakeDiscover{window: platformWindow(6, 30, 7, 50)}
	svc := newService(t, &fakeWindowStore{}, &fakePlanStore{}, fakeAccounts{ids: []string{"a1", "a2", "a3"}}, d)
	in := baseInput()
	in.EdgeFront, in.EdgeBack = 60*time.Second, 60*time.Second
	prev, err := svc.Preview(context.Background(), in)
	if err != nil {
		t.Fatalf("preview: %v", err)
	}
	if !prev.WindowStart.Equal(time.Date(2026, 10, 29, 6, 31, 0, 0, shanghai)) {
		t.Fatalf("有效窗口起点 = %v, want 06:31", prev.WindowStart)
	}
	if !prev.WindowEnd.Equal(time.Date(2026, 10, 29, 7, 49, 0, 0, shanghai)) {
		t.Fatalf("有效窗口终点 = %v, want 07:49", prev.WindowEnd)
	}
}
