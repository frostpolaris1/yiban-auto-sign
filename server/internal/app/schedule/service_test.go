package schedule_test

import (
	"context"
	"errors"
	"reflect"
	"testing"
	"time"

	appevents "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/events"
	appschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schedule"
	corecapacity "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/capacity"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreplan "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/plan"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
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

	override    coreschedule.WindowOverride
	overrideOK  bool
	overrideErr error
	views       []coreschedule.WindowView
	task        coreschedule.TaskInfo
	taskErr     error
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

func (f *fakeWindowStore) GetOverride(context.Context, string, string) (coreschedule.WindowOverride, error) {
	if f.overrideErr != nil {
		return coreschedule.WindowOverride{}, f.overrideErr
	}
	if !f.overrideOK {
		return coreschedule.WindowOverride{}, coreerrs.ErrNotFound
	}
	return f.override, nil
}

func (f *fakeWindowStore) PutOverride(_ context.Context, ov coreschedule.WindowOverride) error {
	f.override, f.overrideOK = ov, true
	return nil
}

func (f *fakeWindowStore) ListWindows(context.Context, corescope.Scope, string) ([]coreschedule.WindowView, error) {
	return f.views, nil
}

func (f *fakeWindowStore) Task(context.Context, string) (coreschedule.TaskInfo, error) {
	if f.taskErr != nil {
		return coreschedule.TaskInfo{}, f.taskErr
	}
	return f.task, nil
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

type fakeAccounts struct {
	ids    []string
	owners map[string]string // 账号 ID 到所有者用户 ID；缺省为空
}

func (f fakeAccounts) ActiveAccounts(context.Context, string) ([]appschedule.AccountRef, error) {
	out := make([]appschedule.AccountRef, len(f.ids))
	for i, id := range f.ids {
		out[i] = appschedule.AccountRef{ID: id, OwnerUserID: f.owners[id]}
	}
	return out, nil
}

// fakePrefs 返回固定偏移，并记录被查询的用户集合。
type fakePrefs struct {
	offsets map[string]int
	err     error
	queried []string
}

func (f *fakePrefs) Offsets(_ context.Context, userIDs []string) (map[string]int, error) {
	f.queried = append(f.queried, userIDs...)
	if f.err != nil {
		return nil, f.err
	}
	out := make(map[string]int, len(f.offsets))
	for _, id := range userIDs {
		if off, ok := f.offsets[id]; ok {
			out[id] = off
		}
	}
	return out, nil
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

// enabledTask 是建计划用例的默认任务夹具：存在且启用，
// 与 baseInput 的 school-1 / task-1 配套。
func enabledTask() coreschedule.TaskInfo {
	return coreschedule.TaskInfo{ID: "task-1", SchoolID: "school-1", Enabled: true}
}

func newService(t *testing.T, w *fakeWindowStore, p *fakePlanStore, a fakeAccounts, d *fakeDiscover) *appschedule.Service {
	t.Helper()
	return newServiceWithPrefs(t, w, p, a, d, nil)
}

func newServiceWithPrefs(t *testing.T, w *fakeWindowStore, p *fakePlanStore, a fakeAccounts, d *fakeDiscover, prefs appschedule.TimePrefs) *appschedule.Service {
	t.Helper()
	svc, err := appschedule.NewService(appschedule.Deps{
		Windows: w, Plans: p, Accounts: a, Discover: d, Prefs: prefs, Tx: &fakeTx{},
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

// 预览就是计划：行、直方图、落库行三者逐值相等。
func TestPreviewEqualsBuild(t *testing.T) {
	w := &fakeWindowStore{task: enabledTask()}
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

// 容量缺口自然降级：按可用容量落库，请求成功；缺口数与建议值仍如实上报。
// 窗口缓存照常写入：worker 的窗口判据不因缺口缺席。
func TestBuildDegradesCapacityShortfall(t *testing.T) {
	ids := make([]string, 100)
	for i := range ids {
		ids[i] = "acct-" + string(rune('a'+i%26)) + string(rune('0'+i/26))
	}
	ws := &fakeWindowStore{task: enabledTask()}
	ps := &fakePlanStore{}
	svc := newService(t, ws, ps, fakeAccounts{ids: ids},
		&fakeDiscover{window: platformWindow(6, 30, 7, 50)})
	in := baseInput()
	in.Workers = 1
	in.Avg = 60 * time.Second // 1 通道、60s/次、80 分钟窗：容量 64，需求 100
	in.Rate = 1.0

	built, err := svc.Build(context.Background(), in)
	if err != nil {
		t.Fatalf("缺口计划应降级成功, err = %v（code %q）", err, coreerrs.From(err).Code)
	}
	if built.Demand != 100 || built.Capacity != 64 || built.Shortfall != 36 {
		t.Fatalf("数字 = demand %d capacity %d shortfall %d, want 100/64/36",
			built.Demand, built.Capacity, built.Shortfall)
	}
	if built.Binding != corecapacity.BindingWorkers || built.SuggestedWorkers != 2 {
		t.Fatalf("建议 = %s/%d, want workers/2", built.Binding, built.SuggestedWorkers)
	}
	// 落库行数 = 可用容量；窗口缓存写入。
	if len(ps.inserted) != 1 || len(ps.inserted[0]) != 64 {
		t.Fatalf("落库批次 = %+v, want 1 批 64 行（能排多少排多少）", ps.inserted)
	}
	if ws.saves != 1 {
		t.Fatalf("窗口缓存写入次数 = %d, want 1", ws.saves)
	}
	// 响应行 = 落库行：行的落点全在窗内，直方图与行一致。
	if len(built.Rows) != 64 {
		t.Fatalf("响应行数 = %d, want 64", len(built.Rows))
	}
	sum := 0
	for _, r := range built.Rows {
		if r.RunAt.Before(built.WindowStart) || !r.RunAt.Before(built.WindowEnd) {
			t.Fatalf("降级行越窗: %v", r.RunAt)
		}
		if built.Histogram[int(r.RunAt.Sub(built.WindowStart)/(5*time.Minute))] <= 0 {
			t.Fatalf("直方图缺行落点: %v", r.RunAt)
		}
		sum++
	}
	if sum != 64 {
		t.Fatalf("可数行 = %d, want 64", sum)
	}
	histSum := 0
	for _, v := range built.Histogram {
		histSum += v
	}
	if histSum != 64 {
		t.Fatalf("直方图总数 = %d, want 64", histSum)
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
	if prev.Binding != corecapacity.BindingWorkers {
		t.Fatalf("Binding = %q, want workers", prev.Binding)
	}
	// 100×60 ÷ (4800×0.8) = 1.5625 → 2 个通道；当前 1 个，故给出建议。
	if prev.SuggestedWorkers != 2 {
		t.Fatalf("SuggestedWorkers = %d, want 2", prev.SuggestedWorkers)
	}
	for _, r := range prev.Rows {
		if r.RunAt.Before(prev.WindowStart) || !r.RunAt.Before(prev.WindowEnd) {
			t.Fatalf("超容量行越窗: %v", r.RunAt)
		}
	}
}

// 出口速率是瓶颈：不给数字建议，降级落库按出口容量截行。
func TestCapacityEgressShortfall(t *testing.T) {
	ids := make([]string, 100)
	for i := range ids {
		ids[i] = "acct-" + string(rune('a'+i%26)) + string(rune('0'+i/26))
	}
	ps := &fakePlanStore{}
	svc := newService(t, &fakeWindowStore{task: enabledTask()}, ps,
		fakeAccounts{ids: ids}, &fakeDiscover{window: platformWindow(6, 30, 6, 31)})
	in := baseInput()
	in.Workers = 4
	in.Avg = 3 * time.Second // 通道吞吐 1.33/s > 出口 0.5/s
	in.Rate = 0.5
	prev, err := svc.Preview(context.Background(), in)
	if err != nil {
		t.Fatalf("preview: %v", err)
	}
	if prev.Binding != corecapacity.BindingEgressRate {
		t.Fatalf("Binding = %q, want egress_rate", prev.Binding)
	}
	// 容量 floor(60×0.5×0.8)=24 < 需求 100；即使理论通道数够也不能建议加并发。
	if prev.SuggestedWorkers != 0 {
		t.Fatalf("出口瓶颈不应给建议, SuggestedWorkers = %d", prev.SuggestedWorkers)
	}

	built, err := svc.Build(context.Background(), in)
	if err != nil {
		t.Fatalf("缺口计划应降级成功: %v", err)
	}
	if built.Binding != corecapacity.BindingEgressRate || built.SuggestedWorkers != 0 {
		t.Fatalf("建议 = %s/%d, want egress_rate/0", built.Binding, built.SuggestedWorkers)
	}
	if len(ps.inserted) != 1 || len(ps.inserted[0]) != 24 {
		t.Fatalf("落库行数 = %+v, want 1 批 24 行（容量 24）", ps.inserted)
	}
}

// 建议值必须真的装得下需求：加通道后撞上出口速率时改报 egress_rate。
func TestCapacitySuggestionRespectsEgressCap(t *testing.T) {
	n := &recordingPlanNotifier{}
	svc := newNotifyingSchedule(t, &fakeWindowStore{task: enabledTask()}, &fakePlanStore{},
		notifyAccounts(1500), &fakeDiscover{window: platformWindow(6, 30, 7, 0)}, n)
	in := baseInput()
	in.Workers = 4
	in.Avg = 10 * time.Second
	in.Rate = 1.0
	prev, err := svc.Preview(context.Background(), in)
	if err != nil {
		t.Fatalf("preview: %v", err)
	}
	// 30 分钟 × min(4/10, 1) × 0.8 = 576；需求 1500，缺口 924。
	if prev.Capacity != 576 || prev.Shortfall != 924 {
		t.Fatalf("capacity/shortfall = %d/%d, want 576/924", prev.Capacity, prev.Shortfall)
	}
	// 公式给出 11 个通道，但 11/10 > 出口 1/s：实得 1440，仍装不下。
	if prev.Binding != corecapacity.BindingEgressRate {
		t.Fatalf("Binding = %q, want egress_rate", prev.Binding)
	}
	if prev.SuggestedWorkers != 0 {
		t.Fatalf("不可达建议不得下发, SuggestedWorkers = %d", prev.SuggestedWorkers)
	}

	_, err = svc.Build(context.Background(), in)
	if err != nil {
		t.Fatalf("缺口计划应降级成功: %v", err)
	}
	// 降级照常落库：首次构建窗口真实变化，window.changed 先行。
	if len(n.events) != 2 || n.events[1].kind != appevents.TypeCapacityChanged {
		t.Fatalf("事件 = %+v, want window.changed + capacity.changed", n.events)
	}
	payload := n.events[1].payload
	if payload["binding"] != string(corecapacity.BindingEgressRate) {
		t.Fatalf("事件 binding = %v, want egress_rate", payload["binding"])
	}
	if _, ok := payload["suggested_workers"]; ok {
		t.Fatalf("事件不得带 suggested_workers: %v", payload)
	}
}

// 建议值超过配置上限时不给：total 79 > 64，保持 workers 但不写数字。
func TestCapacitySuggestionOverCap(t *testing.T) {
	ids := make([]string, 1000)
	for i := range ids {
		ids[i] = "acct-" + string(rune('a'+i%26)) + string(rune('0'+i/26))
	}
	svc := newService(t, &fakeWindowStore{task: enabledTask()}, &fakePlanStore{},
		fakeAccounts{ids: ids}, &fakeDiscover{window: platformWindow(6, 30, 7, 50)})
	in := baseInput()
	in.Workers = 1
	in.Avg = 300 * time.Second // 1000×300 ÷ (4800×0.8) = 78.125 → 79 > 64
	in.Rate = 1.0
	prev, err := svc.Preview(context.Background(), in)
	if err != nil {
		t.Fatalf("preview: %v", err)
	}
	// 1 通道、每次 300 秒、80 分钟：capacity = floor(4800/300×0.8) = 12。
	if prev.Capacity != 12 || prev.Shortfall != 988 {
		t.Fatalf("capacity/shortfall = %d/%d, want 12/988", prev.Capacity, prev.Shortfall)
	}
	if prev.Binding != corecapacity.BindingWorkers {
		t.Fatalf("Binding = %q, want workers", prev.Binding)
	}
	if prev.SuggestedWorkers != 0 {
		t.Fatalf("超过上限不得给建议, SuggestedWorkers = %d", prev.SuggestedWorkers)
	}

	_, err = svc.Build(context.Background(), in)
	if err != nil {
		t.Fatalf("缺口计划应降级成功: %v", err)
	}
}

// 浮点边界：容量恰好等于需求时不得给建议（want 会因浮点算成 2 > 当前 1）。
func TestCapacityAdviceAtExactBoundary(t *testing.T) {
	ids := make([]string, 1260)
	for i := range ids {
		ids[i] = "acct-" + string(rune('a'+i%26)) + string(rune('0'+i/26))
	}
	svc := newService(t, &fakeWindowStore{}, &fakePlanStore{}, fakeAccounts{ids: ids},
		&fakeDiscover{window: platformWindow(6, 30, 8, 0)})
	in := baseInput()
	in.Workers = 1
	in.Avg = 3 * time.Second
	in.Rate = 1.0
	in.Util = 0.7 // 90 分钟 × 1/3 × 0.7 = 1260，恰好等于需求。
	prev, err := svc.Preview(context.Background(), in)
	if err != nil {
		t.Fatalf("preview: %v", err)
	}
	if prev.Capacity != 1260 || prev.Shortfall != 0 {
		t.Fatalf("capacity/shortfall = %d/%d, want 1260/0", prev.Capacity, prev.Shortfall)
	}
	if prev.SuggestedWorkers != 0 {
		t.Fatalf("容量恰好够时不得给建议, SuggestedWorkers = %d", prev.SuggestedWorkers)
	}
}

// 需求装得下时不给建议：want <= 当前 workers，也没有缺口。
func TestCapacitySuggestionSkipsWhenWorkersSuffice(t *testing.T) {
	ids := make([]string, 10)
	for i := range ids {
		ids[i] = "acct-" + string(rune('a'+i%26)) + string(rune('0'+i/26))
	}
	svc := newService(t, &fakeWindowStore{}, &fakePlanStore{}, fakeAccounts{ids: ids},
		&fakeDiscover{window: platformWindow(6, 30, 7, 50)})
	in := baseInput()
	in.Rate = 10.0 // 通道吞吐 2/s 低于出口 10/s：瓶颈在通道，不是出口。
	prev, err := svc.Preview(context.Background(), in)
	if err != nil {
		t.Fatalf("preview: %v", err)
	}
	if prev.Shortfall != 0 || prev.Binding != corecapacity.BindingWorkers {
		t.Fatalf("shortfall/binding = %d/%q, want 0/workers", prev.Shortfall, prev.Binding)
	}
	if prev.SuggestedWorkers != 0 {
		t.Fatalf("通道足够时不得给建议, SuggestedWorkers = %d", prev.SuggestedWorkers)
	}
}

// 窗口**仅起点**变化也必须重排。
func TestWindowStartOnlyChangeRearranges(t *testing.T) {
	w := &fakeWindowStore{task: enabledTask(), ok: true, record: coreschedule.WindowRecord{
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
	w := &fakeWindowStore{task: enabledTask(), ok: true, record: coreschedule.WindowRecord{
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

// 亚毫秒窗口：夹取不得产出早于窗口起点的落点。
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

// 缓存读真错误必须上抛，不得降级为"无缓存"。
func TestCacheReadErrorPropagates(t *testing.T) {
	w := &fakeWindowStore{readErr: errors.New("connection refused")}
	svc := newService(t, w, &fakePlanStore{}, fakeAccounts{ids: []string{"a1"}},
		&fakeDiscover{window: platformWindow(6, 30, 7, 50)})
	if _, err := svc.Preview(context.Background(), baseInput()); err == nil {
		t.Fatal("缓存读真错误应上抛")
	}
}

// 版本竞争：整体重试一次，最终成功且重排一次。
func TestStaleRetriesOnce(t *testing.T) {
	w := &fakeWindowStore{task: enabledTask(), ok: true, staleOnce: true, record: coreschedule.WindowRecord{
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

// 人工覆盖优先于平台窗口：来源标 override，落点按覆盖窗口算。
func TestOverrideWinsOverPlatform(t *testing.T) {
	w := &fakeWindowStore{
		task: coreschedule.TaskInfo{ID: "task-1", SchoolID: "school-1", Name: "sign", Enabled: true},
		override: coreschedule.WindowOverride{
			SchoolTaskID: "task-1", Day: "2026-10-29",
			Start:  time.Date(2026, 10, 29, 9, 0, 0, 0, shanghai),
			End:    time.Date(2026, 10, 29, 9, 30, 0, 0, shanghai),
			Reason: "平台窗口错误",
		},
		overrideOK: true,
	}
	p := &fakePlanStore{}
	d := &fakeDiscover{window: platformWindow(6, 30, 7, 50)}
	svc := newService(t, w, p, fakeAccounts{ids: []string{"a1"}}, d)
	prev, err := svc.Preview(context.Background(), baseInput())
	if err != nil {
		t.Fatalf("preview: %v", err)
	}
	if prev.Source != contract.WindowOverride {
		t.Fatalf("来源 = %s, want override", prev.Source)
	}
	if !prev.WindowStart.Equal(time.Date(2026, 10, 29, 9, 0, 0, 0, shanghai)) {
		t.Fatalf("覆盖窗口起点 = %v, want 09:00", prev.WindowStart)
	}
	if d.calls != 0 {
		t.Fatalf("覆盖存在时不应再问平台, calls=%d", d.calls)
	}
}

// Override 写覆盖、按新窗口重建，并重排 pending。
func TestOverridePersistsAndRebuilds(t *testing.T) {
	w := &fakeWindowStore{
		task: coreschedule.TaskInfo{ID: "task-1", SchoolID: "school-1", Name: "sign", Enabled: true},
	}
	p := &fakePlanStore{}
	d := &fakeDiscover{window: platformWindow(6, 30, 7, 50)}
	svc := newService(t, w, p, fakeAccounts{ids: []string{"a1", "a2"}}, d)
	in := baseInput()
	start := time.Date(2026, 10, 29, 10, 0, 0, 0, shanghai)
	end := time.Date(2026, 10, 29, 11, 0, 0, 0, shanghai)
	prev, err := svc.Override(context.Background(), corescope.Master(), in, start, end, "临时调整", "user-1")
	if err != nil {
		t.Fatalf("Override: %v", err)
	}
	if !w.overrideOK || w.override.Reason != "临时调整" || w.override.CreatedBy != "user-1" {
		t.Fatalf("覆盖未落库: %+v", w.override)
	}
	if prev.Source != contract.WindowOverride || !prev.WindowStart.Equal(start) {
		t.Fatalf("重建未采用覆盖: source=%s start=%v", prev.Source, prev.WindowStart)
	}
	if len(p.rearranged) != 1 || len(p.inserted) != 1 {
		t.Fatalf("重建应重排并补插, rearranged=%d inserted=%d", len(p.rearranged), len(p.inserted))
	}
}

// 覆盖参数非法必须响亮拒绝；跨校任务按 404 处理。
func TestOverrideValidation(t *testing.T) {
	task := coreschedule.TaskInfo{ID: "task-1", SchoolID: "school-1", Name: "sign", Enabled: true}
	start := time.Date(2026, 10, 29, 10, 0, 0, 0, shanghai)
	end := time.Date(2026, 10, 29, 11, 0, 0, 0, shanghai)
	cases := []struct {
		name     string
		scope    corescope.Scope
		start    time.Time
		end      time.Time
		reason   string
		wantCode coreerrs.Code
	}{
		{"理由为空", corescope.Master(), start, end, "  ", coreerrs.CodeValidation},
		{"起点不早于终点", corescope.Master(), end, start, "调整", coreerrs.CodeValidation},
		{"跨业务日", corescope.Master(), start, start.Add(24 * time.Hour), "调整", coreerrs.CodeValidation},
		{"跨校管理员", corescope.Admin([]string{"school-2"}), start, end, "调整", coreerrs.CodeNotFound},
		{"普通用户", corescope.User("u1"), start, end, "调整", coreerrs.CodeForbidden},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			w := &fakeWindowStore{task: task}
			svc := newService(t, w, &fakePlanStore{}, fakeAccounts{}, &fakeDiscover{})
			_, err := svc.Override(context.Background(), c.scope, baseInput(), c.start, c.end, c.reason, "user-1")
			if err == nil {
				t.Fatal("应报错")
			}
			if code := coreerrs.From(err).Code; code != c.wantCode {
				t.Fatalf("错误码 = %q, want %q", code, c.wantCode)
			}
		})
	}
}

// 没有偏好时落点必须与 core/plan.SpreadWithin 逐值相同。
// 这条不变式保证引入自选没有改变既有铺点行为。
func TestNoPrefsMatchesSpreadWithin(t *testing.T) {
	ids := make([]string, 25)
	for i := range ids {
		ids[i] = "acct-" + string(rune('a'+i))
	}
	for _, front := range []bool{true, false} {
		t.Run(map[bool]string{true: "front", false: "uniform"}[front], func(t *testing.T) {
			svc := newService(t, &fakeWindowStore{}, &fakePlanStore{}, fakeAccounts{ids: ids},
				&fakeDiscover{window: platformWindow(6, 30, 7, 50)})
			in := baseInput()
			in.Front = front
			prev, err := svc.Preview(context.Background(), in)
			if err != nil {
				t.Fatalf("preview: %v", err)
			}
			want := coreplan.SpreadWithin(len(ids), in.SlotsPerSlice, 16, front)
			for i, r := range prev.Rows {
				got := r.RunAt.Sub(prev.WindowStart)
				if got != want[i] {
					t.Fatalf("行 %d 偏移 = %v, want %v", i, got, want[i])
				}
			}
		})
	}
}

// 用户自选落到请求的片；其余账号从前面自动铺点，不抢该槽。
func TestTimePrefPlacedFirst(t *testing.T) {
	ids := []string{"a1", "a2", "a3"}
	owners := map[string]string{"a1": "u1", "a2": "u2", "a3": "u3"}
	prefs := &fakePrefs{offsets: map[string]int{"u1": 40}} // 40 分钟 → 8 号片
	svc := newServiceWithPrefs(t, &fakeWindowStore{}, &fakePlanStore{},
		fakeAccounts{ids: ids, owners: owners}, &fakeDiscover{window: platformWindow(6, 30, 7, 50)}, prefs)
	in := baseInput()
	in.UsePrefs = true
	prev, err := svc.Preview(context.Background(), in)
	if err != nil {
		t.Fatalf("preview: %v", err)
	}
	off := prev.Rows[0].RunAt.Sub(prev.WindowStart)
	if off < 40*time.Minute || off >= 45*time.Minute {
		t.Fatalf("自选行偏移 = %v, want [40m,45m)", off)
	}
	if prev.Histogram[8] != 1 {
		t.Fatalf("8 号片人数 = %d, want 1", prev.Histogram[8])
	}
	sorted := prefs.queried
	if len(sorted) != 3 {
		t.Fatalf("应查询 3 个所有者, got %v", sorted)
	}
}

// 偏好片满时就近顺延；同距离优先更早的片。
//
// 偏好放在中部（8 号片）且片容量为 2：前两行占满 8 号片，
// 第三行必须落 7 号片。放在 0 号片时，顺延目标与 front 自动填充
// 的落点重合，「满时不顺延」的突变仍然全绿。
func TestTimePrefSpillsToNearestFree(t *testing.T) {
	ids := []string{"a1", "a2", "a3"}
	owners := map[string]string{"a1": "u1", "a2": "u1", "a3": "u1"}
	prefs := &fakePrefs{offsets: map[string]int{"u1": 40}} // 40 分钟 → 8 号片
	svc := newServiceWithPrefs(t, &fakeWindowStore{}, &fakePlanStore{},
		fakeAccounts{ids: ids, owners: owners}, &fakeDiscover{window: platformWindow(6, 30, 7, 50)}, prefs)
	in := baseInput()
	in.UsePrefs = true
	in.SlotsPerSlice = 2
	prev, err := svc.Preview(context.Background(), in)
	if err != nil {
		t.Fatalf("preview: %v", err)
	}
	want := []int{8, 8, 7}
	for i, k := range want {
		off := prev.Rows[i].RunAt.Sub(prev.WindowStart)
		if off < time.Duration(k)*5*time.Minute || off >= time.Duration(k+1)*5*time.Minute {
			t.Fatalf("行 %d 偏移 = %v, want 片 %d", i, off, k)
		}
	}
	if prev.Histogram[8] != 2 || prev.Histogram[7] != 1 {
		t.Fatalf("直方图 = %v, want 8 号 2 行、7 号 1 行", prev.Histogram)
	}
}

// 部署开关关闭时即便库里有偏好也不读、不生效。
func TestTimePrefDisabledIgnored(t *testing.T) {
	prefs := &fakePrefs{offsets: map[string]int{"u1": 60}}
	svc := newServiceWithPrefs(t, &fakeWindowStore{}, &fakePlanStore{},
		fakeAccounts{ids: []string{"a1"}, owners: map[string]string{"a1": "u1"}},
		&fakeDiscover{window: platformWindow(6, 30, 7, 50)}, prefs)
	prev, err := svc.Preview(context.Background(), baseInput()) // UsePrefs=false
	if err != nil {
		t.Fatalf("preview: %v", err)
	}
	if off := prev.Rows[0].RunAt.Sub(prev.WindowStart); off != 0 {
		t.Fatalf("关闭自选后偏移 = %v, want 0", off)
	}
	if len(prefs.queried) != 0 {
		t.Fatalf("关闭自选后不应查偏好, got %v", prefs.queried)
	}
}

// 列表按作用域下发；user 角色无权查看管理面。
func TestListWindowsScope(t *testing.T) {
	w := &fakeWindowStore{views: []coreschedule.WindowView{{
		SchoolID: "school-1", SchoolName: "一中", TaskID: "task-1", TaskName: "sign",
		Day: "2026-10-29", Start: time.Date(2026, 10, 29, 6, 30, 0, 0, shanghai),
		End:    time.Date(2026, 10, 29, 7, 50, 0, 0, shanghai),
		Source: string(contract.WindowPlatform), Version: 2, Cached: true,
	}}}
	svc := newService(t, w, &fakePlanStore{}, fakeAccounts{}, &fakeDiscover{})
	views, err := svc.ListWindows(context.Background(), corescope.Master(), "2026-10-29", false)
	if err != nil {
		t.Fatalf("ListWindows: %v", err)
	}
	if len(views) != 1 || views[0].TaskID != "task-1" {
		t.Fatalf("views = %+v", views)
	}
	if _, err := svc.ListWindows(context.Background(), corescope.User("u1"), "2026-10-29", false); err == nil {
		t.Fatal("user 角色应被拒绝")
	}
	if _, err := svc.ListWindows(context.Background(), corescope.Master(), "2026/10/29", false); err == nil {
		t.Fatal("非法日期应被拒绝")
	}
}

// Report 带 Refresh 且存在合法覆盖：发现照做，缓存行不改写。
// 依据：缓存行是 worker 的窗口判据（只读缓存）；改写它会让覆盖失效。
func TestReportRefreshKeepsOverrideCache(t *testing.T) {
	ovStart := time.Date(2026, 10, 29, 9, 0, 0, 0, shanghai)
	ovEnd := ovStart.Add(30 * time.Minute)
	w := &fakeWindowStore{
		override: coreschedule.WindowOverride{
			SchoolTaskID: "task-1", Day: "2026-10-29",
			Start: ovStart, End: ovEnd, Reason: "平台窗口错误", CreatedBy: "u1",
		},
		overrideOK: true,
	}
	d := &fakeDiscover{window: platformWindow(6, 30, 7, 50)}
	svc := newService(t, w, &fakePlanStore{}, fakeAccounts{ids: []string{"a1"}}, d)
	in := baseInput()
	in.Refresh = true

	prev, err := svc.Report(context.Background(), in)
	if err != nil {
		t.Fatalf("Report: %v", err)
	}
	if d.calls != 1 {
		t.Fatalf("平台发现 = %d, want 1", d.calls)
	}
	if w.saves != 0 {
		t.Fatalf("覆盖存在时不应改写缓存行: saves = %d", w.saves)
	}
	if prev.Source != contract.WindowOverride || !prev.WindowStart.Equal(ovStart) {
		t.Fatalf("报告 = source %s start %v, want override %v", prev.Source, prev.WindowStart, ovStart)
	}
}

// fakeSelfReader 固定用户读面的账号/任务解析结果。
type fakeSelfReader struct {
	account    coreschedule.SelfAccount
	accountErr error
	rows       []coreschedule.SelfTask
	rowsErr    error
	tasks      []string
	tasksErr   error
}

func (f *fakeSelfReader) ActiveAccountByOwner(context.Context, string) (coreschedule.SelfAccount, error) {
	return f.account, f.accountErr
}

func (f *fakeSelfReader) DayTaskRows(context.Context, string, string) ([]coreschedule.SelfTask, error) {
	return f.rows, f.rowsErr
}

func (f *fakeSelfReader) AccountEnabledTasks(context.Context, string) ([]string, error) {
	return f.tasks, f.tasksErr
}

func newSelfService(t *testing.T, self appschedule.SelfReader) *appschedule.Service {
	t.Helper()
	svc, err := appschedule.NewService(appschedule.Deps{
		Windows: &fakeWindowStore{}, Plans: &fakePlanStore{}, Accounts: fakeAccounts{},
		Discover: &fakeDiscover{}, Self: self, Tx: &fakeTx{},
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

// 自选读面取法：无账号零值；有当日行取第一行（未了结优先由仓储排序）；
// 无当日行取学校启用任务第一条；两者都无 HasTask=false。
func TestSelfContextSelection(t *testing.T) {
	runAt := time.Date(2026, 10, 29, 6, 42, 0, 0, shanghai)
	for _, tc := range []struct {
		name string
		self *fakeSelfReader
		want coreschedule.SelfContext
	}{
		{
			name: "没有生效账号",
			self: &fakeSelfReader{accountErr: coreerrs.ErrNotFound},
			want: coreschedule.SelfContext{},
		},
		{
			name: "当日任务行优先",
			self: &fakeSelfReader{
				account: coreschedule.SelfAccount{ID: "acct-1", SchoolID: "school-1"},
				rows: []coreschedule.SelfTask{
					{SchoolTaskID: "task-next", RunAt: runAt},
					{SchoolTaskID: "task-later", RunAt: runAt.Add(10 * time.Minute)},
				},
			},
			want: coreschedule.SelfContext{
				HasAccount: true, AccountID: "acct-1", SchoolID: "school-1",
				HasTask: true, TaskID: "task-next", RunAt: &runAt,
			},
		},
		{
			name: "无当日行取启用任务",
			self: &fakeSelfReader{
				account: coreschedule.SelfAccount{ID: "acct-1", SchoolID: "school-1"},
				tasks:   []string{"task-first", "task-second"},
			},
			want: coreschedule.SelfContext{
				HasAccount: true, AccountID: "acct-1", SchoolID: "school-1",
				HasTask: true, TaskID: "task-first",
			},
		},
		{
			name: "无启用任务",
			self: &fakeSelfReader{
				account: coreschedule.SelfAccount{ID: "acct-1", SchoolID: "school-1"},
			},
			want: coreschedule.SelfContext{
				HasAccount: true, AccountID: "acct-1", SchoolID: "school-1",
			},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			svc := newSelfService(t, tc.self)
			got, err := svc.SelfContext(context.Background(), "u1", "2026-10-29")
			if err != nil {
				t.Fatalf("SelfContext: %v", err)
			}
			if !reflect.DeepEqual(got, tc.want) {
				t.Fatalf("got %+v, want %+v", got, tc.want)
			}
		})
	}
}

// Self 未装配时读面保持零值，调用方不会拿到半套账号上下文。
func TestSelfContextWithoutReader(t *testing.T) {
	svc := newSelfService(t, nil)
	got, err := svc.SelfContext(context.Background(), "u1", "2026-10-29")
	if err != nil {
		t.Fatalf("SelfContext: %v", err)
	}
	if got != (coreschedule.SelfContext{}) {
		t.Fatalf("got %+v, want zero", got)
	}
}
