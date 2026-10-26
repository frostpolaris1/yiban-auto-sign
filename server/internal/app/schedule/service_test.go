package schedule_test

import (
	"context"
	"errors"
	"testing"
	"time"

	appschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schedule"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// ---- 测试替身（模拟器在"计划落点与窗口来源"这一维等价：状态机真实、无 I/O）----

type fakeWindowStore struct {
	record  coreschedule.WindowRecord
	ok      bool
	saves   int
	lastExp int
}

func (f *fakeWindowStore) LoadWindow(_ context.Context, taskID, day string) (coreschedule.WindowRecord, error) {
	if !f.ok {
		return coreschedule.WindowRecord{}, errors.New("no cached window")
	}
	f.record.SchoolTaskID, f.record.Day = taskID, day
	return f.record, nil
}

func (f *fakeWindowStore) SaveWindow(_ context.Context, w coreschedule.WindowRecord, expectedVersion int) (int, error) {
	f.lastExp = expectedVersion
	if expectedVersion > 0 && expectedVersion != f.record.Version {
		return 0, coreschedule.ErrWindowStale
	}
	version := f.record.Version + 1
	if expectedVersion == 0 && f.ok {
		version = f.record.Version + 1
	}
	w.Version = version
	f.record, f.ok = w, true
	f.saves++
	return version, nil
}

type fakePlanStore struct {
	inserted   [][]coreschedule.PlanRow
	rearranged [][]appschedule.Row
	// existing 模拟库内已有行：重放时返回 0 写入。
	existing map[string]bool
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

func (f *fakePlanStore) RearrangePlan(_ context.Context, taskID, day string, rows []appschedule.Row) (int64, error) {
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

type fixedClock struct{ t time.Time }

func (f fixedClock) Now() time.Time { return f.t }

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

func platformWindow(startH, startM, endH, endM int) contract.Window {
	return contract.Window{
		Start:  time.Date(2026, 10, 29, startH, startM, 0, 0, shanghai),
		End:    time.Date(2026, 10, 29, endH, endM, 0, 0, shanghai),
		Source: contract.WindowPlatform,
	}
}

func newService(t *testing.T, w *fakeWindowStore, p *fakePlanStore, a fakeAccounts, d *fakeDiscover) *appschedule.Service {
	t.Helper()
	svc, err := appschedule.NewService(appschedule.Deps{
		Windows: w, Plans: p, Accounts: a, Discover: d,
		Clock: coreclock.Fixed{T: time.Date(2026, 10, 29, 5, 0, 0, 0, shanghai)},
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

// 预览就是计划：同一构建函数，行与直方图逐值相等；Build 落库的正是预览行。
func TestPreviewEqualsBuild(t *testing.T) {
	w := &fakeWindowStore{}
	p := &fakePlanStore{}
	accts := fakeAccounts{ids: []string{"a1", "a2", "a3", "a4", "a5"}}
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
	if len(prev.Rows) != len(built.Rows) || len(built.Rows) != 5 {
		t.Fatalf("行数: preview=%d build=%d want 5", len(prev.Rows), len(built.Rows))
	}
	for i := range prev.Rows {
		if prev.Rows[i].AccountID != built.Rows[i].AccountID || !prev.Rows[i].RunAt.Equal(built.Rows[i].RunAt) {
			t.Fatalf("行 %d 不同源: %+v vs %+v", i, prev.Rows[i], built.Rows[i])
		}
	}
	if len(prev.Histogram) != len(built.Histogram) {
		t.Fatalf("直方图不同源: %v vs %v", prev.Histogram, built.Histogram)
	}
	if len(p.inserted) != 1 || len(p.inserted[0]) != 5 {
		t.Fatalf("落库行数 = %+v, want 5", p.inserted)
	}
	// 首次构建：窗口写入一次，重排一次（窗口从无到有算变化）。
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

	inA := baseInput()
	prevA, err := svc.Preview(context.Background(), inA)
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
	// 平台可用 → platform。
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
	// 平台失败 + 缓存可用 → cache。
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
	// 平台与缓存都不可用 → fallback（06:30~07:50）。
	d3 := &fakeDiscover{err: errors.New("upstream down")}
	svc = newService(t, &fakeWindowStore{}, &fakePlanStore{}, fakeAccounts{ids: []string{"a1"}}, d3)
	prev, err = svc.Preview(context.Background(), baseInput())
	if err != nil {
		t.Fatalf("preview fallback: %v", err)
	}
	if prev.Source != contract.WindowFallback {
		t.Fatalf("来源 = %s, want fallback", prev.Source)
	}
	if !prev.WindowStart.Equal(time.Date(2026, 10, 29, 6, 30, 0, 0, shanghai)) {
		t.Fatalf("回退窗口起点 = %v, want 06:30", prev.WindowStart)
	}
}

// 容量缺口如实上报；超容量的行仍在窗内（顺延不丢号）。
func TestCapacityShortfallReported(t *testing.T) {
	w := &fakeWindowStore{}
	p := &fakePlanStore{}
	ids := make([]string, 100)
	for i := range ids {
		ids[i] = "acct-" + string(rune('a'+i%26)) + string(rune('0'+i/26))
	}
	d := &fakeDiscover{window: platformWindow(6, 30, 7, 50)}
	svc := newService(t, w, p, fakeAccounts{ids: ids}, d)
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

// 窗口变化时重排 pending：保存新版本并调用 RearrangePlan。
func TestWindowChangeRearranges(t *testing.T) {
	w := &fakeWindowStore{ok: true, record: coreschedule.WindowRecord{
		Start:  time.Date(2026, 10, 29, 6, 30, 0, 0, shanghai),
		End:    time.Date(2026, 10, 29, 7, 50, 0, 0, shanghai),
		Source: string(contract.WindowPlatform), Version: 1,
	}}
	p := &fakePlanStore{}
	d := &fakeDiscover{window: platformWindow(7, 0, 8, 0)} // 平台窗口变了
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
		t.Fatalf("窗口变化应重排一次, got %d", len(p.rearranged))
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
	// 二次构建：同一批行再插，写入 0（模拟库唯一约束）。
	if _, err := svc.Build(context.Background(), baseInput()); err != nil {
		t.Fatalf("second build: %v", err)
	}
	if len(p.inserted) != 2 {
		t.Fatalf("第二次应有调用记录, got %d", len(p.inserted))
	}
}

// 输入校验：非法参数必须响亮拒绝（不静默补缺省）。
func TestInvalidInput(t *testing.T) {
	svc := newService(t, &fakeWindowStore{}, &fakePlanStore{}, fakeAccounts{}, &fakeDiscover{})
	cases := []func(*appschedule.Input){
		func(in *appschedule.Input) { in.SchoolID = "" },
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

// 有效窗口裁剪：EdgeFront/EdgeBack 生效，落点不落在缓冲带。
func TestEdgeTrimApplied(t *testing.T) {
	w := &fakeWindowStore{}
	d := &fakeDiscover{window: platformWindow(6, 30, 7, 50)}
	svc := newService(t, w, &fakePlanStore{}, fakeAccounts{ids: []string{"a1", "a2", "a3"}}, d)
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
