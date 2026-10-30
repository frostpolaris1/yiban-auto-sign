// Package schedule 实现计划构建与预览用例（P4-C）。
//
// # 功能
// 解析当日窗口（平台 > 缓存 > 回退），构建账号落点计划，
// 幂等落库并在窗口版本变化时重排未执行的计划行。
// Preview 与 Build 共用同一个构建函数：预览就是计划本身，不是另一套估算。
// 本包**不**发 HTTP 请求、**不**写 SQL、**不**实现入口端点。
//
// # 归属
// app 层用例。端口见 repo.go：存储类接 adapters/pg，
// Discoverer 由 P4-F 用 signing provider 接线（本批无实现）。
//
// # 复用
// Service、Input、Preview、Row；端口 WindowStore、PlanStore、Accounts、Discoverer。
//
// # 口径
// 业务日按 Input.Location 解析；窗口来源取 contract.WindowSource 三值。
// 预览、建计划与写覆盖用实时窗口（先问平台）。
// 只读报告（Report、ListWindows）默认只读缓存；Refresh 为真才重新发现（k0y-4）。
// 停用任务可预览，不可建计划与写覆盖：回 conflict（k0y-6）。
package schedule

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"
	"unicode/utf8"

	appcore "github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appevents "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/events"
	corecapacity "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/capacity"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
	coreplan "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/plan"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	corewindow "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/window"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// fallbackStartHM / fallbackEndHM 是平台与缓存都不可用时的回退窗口（06:30~07:50）。
// 只是作者部署的典型时段；来源会标 fallback，UI 必须显示。
const (
	fallbackStartHM = 6*60 + 30
	fallbackEndHM   = 7*60 + 50
)

// Input 是一次计划构建的输入。
type Input struct {
	SchoolID     string
	SchoolTaskID string
	Day          string // YYYY-MM-DD 业务日
	Location     *time.Location
	// Workers 是并发通道数；Avg 是单账号尝试耗时。
	Workers int
	Avg     time.Duration
	// Rate 是出口速率（attempt/s）；Util 是容量利用系数。
	Rate float64
	Util float64
	// SlotsPerSlice 是每个 5 分钟片的账号槽数。
	SlotsPerSlice int
	// Front 为真时顺序装满前段；为假时全窗分层。
	Front bool
	// UsePrefs 为真时读取并应用用户自选偏移。
	// 它是部署开关：关掉后已存偏好不再影响计划。
	UsePrefs bool
	// EdgeFront / EdgeBack 是有效窗口两端让出的时长；0 表示不裁剪。
	EdgeFront time.Duration
	EdgeBack  time.Duration
	// Refresh 要求重新发现平台窗口（k0y-4）。
	// 只对只读报告生效：Report 由入口按 refresh 查询参数置位。
	// Preview 与 Build 恒查平台，忽略本字段。
	Refresh bool
}

// Row 是一行计划落点。
type Row struct {
	AccountID string
	RunAt     time.Time
}

// Preview 是计划的可预览结果：同一构建函数产出，Build 落库的就是这些行。
type Preview struct {
	Day         string
	Source      contract.WindowSource
	WindowStart time.Time
	WindowEnd   time.Time
	// Version 在 Preview 里是当前缓存版本；Build 返回的是落库后版本。
	Version   int
	Capacity  int
	Demand    int
	Shortfall int
	// Histogram 是 5 分钟片的落点计数，键为片号（相对有效窗口起点）。
	Histogram map[int]int
	Rows      []Row
}

// Service 是计划用例服务。
type Service struct {
	deps Deps
	log  *slog.Logger
}

// Notifier 是计划事件的发布端口（实现为 app/events.Broker 的适配器）。
// kind 取 app/events 的 Type 词表。为 nil 时不发布，现行为不变。
// 实现必须不阻塞；实现 panic 视为发布失败，只丢事件（见 publish）。
type Notifier interface {
	PlanEvent(ctx context.Context, schoolID, taskID, day string, kind string, payload map[string]any)
}

// NewService 构造服务。缺依赖即失败。
func NewService(deps Deps) (*Service, error) {
	if deps.Windows == nil {
		return nil, errors.New("schedule: nil window store")
	}
	if deps.Plans == nil {
		return nil, errors.New("schedule: nil plan store")
	}
	if deps.Accounts == nil {
		return nil, errors.New("schedule: nil account source")
	}
	if deps.Discover == nil {
		return nil, errors.New("schedule: nil window discoverer")
	}
	if deps.Tx == nil {
		return nil, errors.New("schedule: nil transaction manager")
	}
	log := deps.Logger
	if log == nil {
		log = slog.Default()
	}
	return &Service{deps: deps, log: log}, nil
}

// Preview 只算不写：返回窗口解析结果与完整计划行。
// 与 Build 同一解析口径（预览就是计划），故不节流；Refresh 不影响本方法。
// 停用任务同样可预览（k0y-6）。
func (s *Service) Preview(ctx context.Context, in Input) (Preview, error) {
	p, _, err := s.previewWithWindow(ctx, in, policyLive)
	return p, err
}

// Report 计算容量报告所需的计划指标（k0y-4）。
//
// 窗口解析默认只读缓存：有当日缓存时零平台调用。
// 无缓存时发现一次并落缓存；Refresh 为真时也发现一次。
// 两个只读端点对 refresh=true 行为一致：都做平台发现。
// 存在合法覆盖时不写缓存（见 storeFresh）：缓存窗口保持覆盖行。
// 报告采用的窗口仍以覆盖优先；与计划同一口径。
// 停用任务同样可报告容量（k0y-6）。
func (s *Service) Report(ctx context.Context, in Input) (Preview, error) {
	p, res, err := s.previewWithWindow(ctx, in, policyCached)
	if err != nil {
		return Preview{}, err
	}
	version, err := s.storeFresh(ctx, res)
	if err != nil {
		return Preview{}, err
	}
	p.Version = version
	return p, nil
}

// storeFresh 把本次新发现的平台窗口写进当日缓存，返回生效版本。
// 只写 fresh：回退窗口不是发现结果，覆盖不是本路径的写入对象。
// 存在合法覆盖时不写库：缓存行是 worker 的窗口判据（只读缓存），
// 改写它会让覆盖失效，pending 行可能被按平台窗口判终态。
// 与缓存同值同源时不写库：刷新是幂等的，不推版本。
// 版本竞争（ErrWindowStale）不算失败：并发写者已写入平台窗口，
// 本次以读到的缓存版本为准。
func (s *Service) storeFresh(ctx context.Context, res windowResolution) (int, error) {
	version := res.version()
	if !res.freshOK || res.overrideOK {
		return version, nil
	}
	if res.cacheOK && res.cached.Source == res.fresh.Source &&
		res.cached.Start.Equal(res.fresh.Start) && res.cached.End.Equal(res.fresh.End) {
		return version, nil
	}
	saved, err := s.deps.Windows.PutWindow(ctx, res.fresh, version)
	if errors.Is(err, coreschedule.ErrWindowStale) {
		return version, nil
	}
	if err != nil {
		return version, err
	}
	return saved, nil
}

// Build 构建计划并落库：窗口变化时同事务内写窗口、重排 pending、补插。
// 版本竞争（ErrWindowStale）时整体重试一次；仍失败即上抛。
// 事务由本方法自开：入口层不得再包一层事务（提交时点会随之改变）。
// 成功且窗口真实变化后发 window.changed。
// 容量缺口拒建（C2）：回 409 capacity_shortfall，**一行都不落库**，
// 并先发 capacity.changed 让订阅方看到缺口数（见 publishCapacity）。
// 停用任务拒建计划（k0y-6）：回 conflict，且在发现平台窗口之前拒绝。
func (s *Service) Build(ctx context.Context, in Input) (Preview, error) {
	if err := validateInput(in); err != nil {
		return Preview{}, err
	}
	if err := s.requireEnabled(ctx, in.SchoolTaskID); err != nil {
		return Preview{}, err
	}
	p, changed, err := s.buildOnce(ctx, in)
	if errors.Is(err, coreschedule.ErrWindowStale) {
		// 并发写者先进：重读缓存后整体重试一次。
		p, changed, err = s.buildOnce(ctx, in)
	}
	if err != nil {
		// 缺口路径带回已算好的 Preview；其余错误是零值，事件自然不发。
		s.publishCapacity(ctx, in, p)
		return Preview{}, err
	}
	s.publishWindow(ctx, in, p, changed)
	return p, nil
}

// requireEnabled 读任务并拒绝停用任务。
// 任务不存在时 ErrNotFound 原样上抛：入口层已先判作用域。
func (s *Service) requireEnabled(ctx context.Context, taskID string) error {
	task, err := s.deps.Windows.Task(ctx, taskID)
	if err != nil {
		return err
	}
	return checkEnabled(task)
}

// checkEnabled 是停用判据的唯一实现：建计划与写覆盖共用（k0y-6）。
// 停用是"任务存在但不可操作"，故回 conflict，不回 not_found；
// 后者留给跨校与不存在，两者口径必须分开。
func checkEnabled(task coreschedule.TaskInfo) error {
	if task.Enabled {
		return nil
	}
	return coreerrs.New(coreerrs.CodeConflict, "任务已停用")
}

// Task 按作用域读任务元数据。
// 跨校与不存在一律 404：不区分"没有"与"不给你看"。
// 停用不影响读取：Enabled 原样下发给调用方；拒绝对停用任务的操作见 checkEnabled。
func (s *Service) Task(ctx context.Context, scope corescope.Scope, taskID string) (coreschedule.TaskInfo, error) {
	if err := requireScheduleScope(scope); err != nil {
		return coreschedule.TaskInfo{}, err
	}
	if taskID == "" {
		return coreschedule.TaskInfo{}, validation("task is required")
	}
	task, err := s.deps.Windows.Task(ctx, taskID)
	if err != nil {
		return coreschedule.TaskInfo{}, err
	}
	if !scopeAllowsSchool(scope, task.SchoolID) {
		return coreschedule.TaskInfo{}, coreerrs.New(coreerrs.CodeNotFound, "school task not found")
	}
	return task, nil
}

// ListWindows 返回作用域内各任务当日的窗口。
// 没有缓存的任务也返回一行（Cached=false）：管理面要看到"未发现"。
// 默认只读缓存（k0y-4）：有缓存的任务零平台调用。
// 无缓存的任务发现一次并落缓存；refresh 为真时全部重新发现。
func (s *Service) ListWindows(ctx context.Context, scope corescope.Scope, day string, refresh bool) ([]coreschedule.WindowView, error) {
	if err := requireScheduleScope(scope); err != nil {
		return nil, err
	}
	if !validDay(day) {
		return nil, validation("day must be YYYY-MM-DD")
	}
	views, err := s.deps.Windows.ListWindows(ctx, scope, day)
	if err != nil {
		return nil, err
	}
	for i := range views {
		if views[i].Cached && !refresh {
			continue
		}
		if err := s.refreshView(ctx, &views[i], day); err != nil {
			return nil, err
		}
	}
	return views, nil
}

// refreshView 发现一次平台窗口，落缓存并就地刷新列表行。
// 平台无窗口或发现失败时保留原行：不落回退窗口（见 storeFresh）。
// 存在合法覆盖时只完成发现、不改写缓存行：缓存行是 worker 的
// 窗口判据，改写它会让覆盖失效（同 storeFresh 的判据）。
// 与缓存同值同源时不写库：刷新是幂等的，不推版本。
// 版本竞争（ErrWindowStale）时保留读到的快照：并发写者已给出更新值。
func (s *Service) refreshView(ctx context.Context, v *coreschedule.WindowView, day string) error {
	found, err := s.deps.Discover.DiscoverWindow(ctx, v.TaskID)
	if err != nil {
		return nil
	}
	if corewindow.Validate(corewindow.Window{Start: found.Start, End: found.End}) != nil {
		return nil
	}
	active, err := s.overrideActive(ctx, v.TaskID, day)
	if err != nil {
		return err
	}
	if active {
		return nil
	}
	rec := coreschedule.WindowRecord{
		SchoolTaskID: v.TaskID, Day: day,
		Start: found.Start, End: found.End,
		Source: string(contract.WindowPlatform),
	}
	if v.Cached && v.Source == rec.Source && v.Start.Equal(rec.Start) && v.End.Equal(rec.End) {
		return nil
	}
	expected := 0
	if v.Cached {
		expected = v.Version
	}
	version, err := s.deps.Windows.PutWindow(ctx, rec, expected)
	if errors.Is(err, coreschedule.ErrWindowStale) {
		return nil
	}
	if err != nil {
		return err
	}
	v.Start, v.End, v.Source = rec.Start, rec.End, rec.Source
	v.Version, v.Cached = version, true
	return nil
}

// overrideActive 报告某任务某日是否存在合法人工覆盖。
// 判据与 resolveWindow 同源：覆盖存在且窗口形状合法。
// 读失败除"没有覆盖"外一律上抛，不把错误当成无覆盖。
func (s *Service) overrideActive(ctx context.Context, taskID, day string) (bool, error) {
	ov, err := s.deps.Windows.GetOverride(ctx, taskID, day)
	if err != nil {
		if isNotFound(err) {
			return false, nil
		}
		return false, err
	}
	return corewindow.Validate(corewindow.Window{Start: ov.Start, End: ov.End}) == nil, nil
}

// Override 写一条人工窗口覆盖，并按新窗口重建当日计划。
//
// 覆盖优先级最高：同任务同日存在覆盖时不再采信平台窗口。
// 覆盖写库与计划重建分两步；重建走 buildOnce，因此窗口变化检测、
// pending 重排与补插仍只有一条实现路径。
// **重建失败时覆盖意图已落库，会留下部分状态**：覆盖是持久意图，下一次
// Build 自愈；调用方遇到失败应重试，不要以为覆盖没生效（k0y-2）。
// 停用任务拒写覆盖（k0y-6）：判据在写库之前，回 conflict 且不留覆盖行。
func (s *Service) Override(ctx context.Context, scope corescope.Scope, in Input, start, end time.Time, reason, actorID string) (Preview, error) {
	if err := requireScheduleScope(scope); err != nil {
		return Preview{}, err
	}
	if err := validateInput(in); err != nil {
		return Preview{}, err
	}
	task, err := s.Task(ctx, scope, in.SchoolTaskID)
	if err != nil {
		return Preview{}, err
	}
	if task.SchoolID != in.SchoolID {
		return Preview{}, validation("task does not belong to the school")
	}
	if err := checkEnabled(task); err != nil {
		return Preview{}, err
	}
	reason = strings.TrimSpace(reason)
	if n := utf8.RuneCountInString(reason); n < 1 || n > 200 {
		return Preview{}, validation("reason must be 1 to 200 characters")
	}
	if !start.Before(end) {
		return Preview{}, validation("override start must be before end")
	}
	if !sameBusinessDay(start, in.Day, in.Location) || !sameBusinessDay(end, in.Day, in.Location) {
		return Preview{}, validation("override start and end must fall on the business day")
	}
	if err := s.deps.Windows.PutOverride(ctx, coreschedule.WindowOverride{
		SchoolTaskID: in.SchoolTaskID,
		Day:          in.Day,
		Start:        start,
		End:          end,
		Reason:       reason,
		CreatedBy:    actorID,
	}); err != nil {
		return Preview{}, err
	}
	p, changed, err := s.buildOnce(ctx, in)
	if errors.Is(err, coreschedule.ErrWindowStale) {
		p, changed, err = s.buildOnce(ctx, in)
	}
	if err != nil {
		return Preview{}, err
	}
	// 覆盖同样可能改变容量（窗口变短/账号变多）：与 Build 同一口径
	//（P6 审查 P3-1）。
	s.publishWindow(ctx, in, p, changed)
	s.publishCapacity(ctx, in, p)
	return p, nil
}

// sameBusinessDay 报告 t 在 loc 下是否为 day（YYYY-MM-DD）。
func sameBusinessDay(t time.Time, day string, loc *time.Location) bool {
	return t.In(loc).Format("2006-01-02") == day
}

// validDay 检查业务日形状。日期本身由 time.ParseInLocation 在构建时校验。
func validDay(day string) bool {
	if len(day) != len("2006-01-02") {
		return false
	}
	_, err := time.Parse("2006-01-02", day)
	return err == nil
}

// requireScheduleScope 限定计划管理面只给 master 与 admin。
func requireScheduleScope(scope corescope.Scope) error {
	if err := appcore.RequireScope(scope); err != nil {
		return err
	}
	switch scope.Kind() {
	case corescope.KindMaster, corescope.KindAdmin:
		return nil
	default:
		return coreerrs.New(coreerrs.CodeForbidden, "role cannot manage schedules")
	}
}

// scopeAllowsSchool 报告 scope 是否可管理该院校。
// admin 的空绑定列表看不到任何院校：空集合不等于全部。
func scopeAllowsSchool(scope corescope.Scope, schoolID string) bool {
	if scope.Kind() == corescope.KindMaster {
		return true
	}
	for _, id := range scope.SchoolIDs() {
		if id == schoolID {
			return true
		}
	}
	return false
}

// buildOnce 是一次完整尝试：预览 → 容量门 → 同事务写入。
// 返回值带 changed：窗口是否真实重写（发布判据用）。
// 容量缺口在写库之前拒绝：Preview 原样带回，供调用方报缺口数。
func (s *Service) buildOnce(ctx context.Context, in Input) (Preview, bool, error) {
	p, res, err := s.previewWithWindow(ctx, in, policyLive)
	if err != nil {
		return Preview{}, false, err
	}
	if p.Shortfall > 0 {
		// 超卖计划不落库：尾段账号必然落空，还让管理员以为已排好。
		return p, false, shortfallError(p)
	}
	changed := res.changed()
	rows := planRows(in, p.Rows)
	err = s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		if changed {
			version, err := s.deps.Windows.PutWindow(ctx, res.effective, res.version())
			if err != nil {
				return err
			}
			p.Version = version
			if _, err := s.deps.Plans.RearrangePlan(ctx, in.SchoolTaskID, in.Day, rows); err != nil {
				return err
			}
		}
		_, err := s.deps.Plans.InsertPlan(ctx, rows)
		return err
	})
	if err != nil {
		return Preview{}, false, err
	}
	return p, changed, nil
}

// shortfallError 把容量缺口翻成 409 capacity_shortfall。
// details 带需求、容量与缺口三个数：调用方不必重算，
// 也不必从 message 里抠数字。
func shortfallError(p Preview) error {
	err := coreerrs.New(coreerrs.CodeCapacityShortfall,
		fmt.Sprintf("容量不足：需求 %d，容量 %d", p.Demand, p.Capacity))
	err.Details = map[string]any{
		"demand":    p.Demand,
		"capacity":  p.Capacity,
		"shortfall": p.Shortfall,
	}
	return err
}

// publishWindow 在窗口真实变化后发 window.changed。
// 未变化的重算不产生事件：管理员视角下没有新信息。
func (s *Service) publishWindow(ctx context.Context, in Input, p Preview, changed bool) {
	if !changed || s.deps.Notifier == nil {
		return
	}
	defer func() { _ = recover() }() // 发布失败只丢事件
	s.deps.Notifier.PlanEvent(ctx, in.SchoolID, in.SchoolTaskID, in.Day,
		appevents.TypeWindowChanged, map[string]any{
			"source":  string(p.Source),
			"version": p.Version,
			"start":   p.WindowStart,
			"end":     p.WindowEnd,
		})
}

// publishCapacity 在出现容量缺口后发 capacity.changed。
// 口径偏差（已申报）：服务无历史 Shortfall 可比，取「Shortfall>0 即发」；
// 缺口持续时会重复发布，管理员侧按幂等处理。
func (s *Service) publishCapacity(ctx context.Context, in Input, p Preview) {
	if p.Shortfall <= 0 || s.deps.Notifier == nil {
		return
	}
	defer func() { _ = recover() }() // 发布失败只丢事件
	s.deps.Notifier.PlanEvent(ctx, in.SchoolID, in.SchoolTaskID, in.Day,
		appevents.TypeCapacityChanged, map[string]any{
			"capacity":  p.Capacity,
			"demand":    p.Demand,
			"shortfall": p.Shortfall,
		})
}

// windowPolicy 是窗口解析口径。
type windowPolicy int

const (
	// policyLive 先问平台：预览、建计划与写覆盖都用实时窗口。
	policyLive windowPolicy = iota
	// policyCached 默认读缓存：只读报告用（k0y-4）。
	// 有当日缓存时零平台调用；无缓存或 Input.Refresh 为真时才发现一次。
	policyCached
)

// windowResolution 是一次窗口解析的结果。
// effective 是报告与计划采用的窗口：覆盖 > 平台 > 缓存 > 回退。
// fresh 是本次新发现的平台窗口；freshOK 为真时待落缓存。
// cached 是当日缓存行；cacheOK 为假时无意义。
// overrideOK 为真表示存在合法人工覆盖。
type windowResolution struct {
	effective  coreschedule.WindowRecord
	cached     coreschedule.WindowRecord
	cacheOK    bool
	fresh      coreschedule.WindowRecord
	freshOK    bool
	overrideOK bool
}

// version 返回当日缓存版本；无缓存为 0。
func (r windowResolution) version() int {
	if !r.cacheOK {
		return 0
	}
	return r.cached.Version
}

// changed 报告 effective 与当日缓存是否不同；无缓存即不同。
// 判据与 PutWindow + RearrangePlan 的触发口径同源（Build 用）。
func (r windowResolution) changed() bool {
	if !r.cacheOK {
		return true
	}
	return !r.cached.Start.Equal(r.effective.Start) ||
		!r.cached.End.Equal(r.effective.End) ||
		r.cached.Source != r.effective.Source
}

// previewWithWindow 返回预览与本次窗口解析结果。
func (s *Service) previewWithWindow(ctx context.Context, in Input, policy windowPolicy) (Preview, windowResolution, error) {
	if err := validateInput(in); err != nil {
		return Preview{}, windowResolution{}, err
	}
	loc := in.Location
	dayDate, err := time.ParseInLocation("2006-01-02", in.Day, loc)
	if err != nil {
		return Preview{}, windowResolution{}, validation("day must be YYYY-MM-DD")
	}
	res, err := s.resolveWindow(ctx, in, dayDate, loc, policy)
	if err != nil {
		return Preview{}, windowResolution{}, err
	}
	eff, _ := corewindow.Window{Start: res.effective.Start, End: res.effective.End}.
		Effective(in.EdgeFront, in.EdgeBack)
	if err := corewindow.Validate(eff); err != nil {
		return Preview{}, windowResolution{}, validation("window has no positive span: " + err.Error())
	}
	p, err := s.buildRows(ctx, in, eff, res.effective)
	if err != nil {
		return Preview{}, windowResolution{}, err
	}
	p.Version = res.version()
	return p, res, nil
}

// resolveWindow 按覆盖 > 平台 > 缓存 > 回退解析当日窗口。
// 平台发现失败不算错误（来源如实标注）；缓存读取失败**只有**
// errs.ErrNotFound 才算无缓存，其他错误上抛（P4-C 复审 P2-5）。
// 人工覆盖存在且合法时优先：覆盖是运维对当日窗口的明确决定。
// 何时问平台：policyLive 且无合法覆盖；policyCached 且无缓存或 Refresh。
// policyCached 的刷新先于覆盖判定：两个只读端点对 refresh=true
// 行为一致，都发现平台窗口；存在覆盖时调用方不改写缓存行。
func (s *Service) resolveWindow(ctx context.Context, in Input, day time.Time, loc *time.Location, policy windowPolicy) (windowResolution, error) {
	var res windowResolution
	cached, cacheErr := s.deps.Windows.GetWindow(ctx, in.SchoolTaskID, in.Day)
	if cacheErr != nil && !isNotFound(cacheErr) {
		return res, cacheErr
	}
	res.cached = cached
	res.cacheOK = cacheErr == nil &&
		corewindow.Validate(corewindow.Window{Start: cached.Start, End: cached.End}) == nil
	override, ovErr := s.deps.Windows.GetOverride(ctx, in.SchoolTaskID, in.Day)
	if ovErr != nil && !isNotFound(ovErr) {
		return res, ovErr
	}
	res.overrideOK = ovErr == nil &&
		corewindow.Validate(corewindow.Window{Start: override.Start, End: override.End}) == nil

	discover := false
	if policy == policyCached {
		discover = in.Refresh || !res.cacheOK
	} else {
		discover = !res.overrideOK
	}
	if discover {
		platform, discoverErr := s.deps.Discover.DiscoverWindow(ctx, in.SchoolTaskID)
		if discoverErr == nil {
			if verr := corewindow.Validate(corewindow.Window{Start: platform.Start, End: platform.End}); verr == nil {
				res.fresh = coreschedule.WindowRecord{
					SchoolTaskID: in.SchoolTaskID, Day: in.Day,
					Start: platform.Start, End: platform.End,
					Source: string(contract.WindowPlatform),
				}
				res.freshOK = true
			} else {
				s.logWindowFailure(ctx, in, "", mask.MaskText(verr.Error()))
			}
		} else {
			outcome, reason := windowFailure(discoverErr)
			s.logWindowFailure(ctx, in, outcome, reason)
		}
	}

	switch {
	case res.overrideOK:
		res.effective = coreschedule.WindowRecord{
			SchoolTaskID: in.SchoolTaskID, Day: in.Day,
			Start: override.Start, End: override.End,
			Source: string(contract.WindowOverride),
		}
	case res.freshOK:
		res.effective = res.fresh
	case res.cacheOK:
		res.effective = res.cached
	default:
		res.effective = coreschedule.WindowRecord{
			SchoolTaskID: in.SchoolTaskID, Day: in.Day,
			Start:  fallbackAt(day, loc, fallbackStartHM),
			End:    fallbackAt(day, loc, fallbackEndHM),
			Source: string(contract.WindowFallback),
		}
	}
	return res, nil
}

// logWindowFailure 记录一次平台窗口发现失败。
// 发现失败只回退、不报错；不记日志就没有任何痕迹（OCR M1）。
// 级别取 app.OutcomeLogLevel：已签到这类正常结论 Info，其余 Warn。
// 没有结论词的失败（窗口非法、传输错误）一律 Warn：它们都需要人看。
// reason 先过 mask.MaskText：日志与审计共用同一套掩码口径。
func (s *Service) logWindowFailure(ctx context.Context, in Input, outcome, reason string) {
	level := slog.LevelWarn
	if outcome != "" {
		level = appcore.OutcomeLogLevel(contract.Outcome(outcome))
	}
	s.log.Log(ctx, level, "window discovery failed",
		"school_task_id", in.SchoolTaskID,
		"day", in.Day,
		"outcome", outcome,
		"reason", mask.MaskText(reason),
	)
}

// windowFailure 把发现失败拆成结论词与可进日志的原因。
// OutcomeError 的 Message 由提供方给出且已掩码，可直接回放；
// 其余错误只给分类词：原文带完整 URL，查询串里有会话标识。
func windowFailure(err error) (string, string) {
	var outcomeErr *contract.OutcomeError
	if errors.As(err, &outcomeErr) {
		return string(outcomeErr.Outcome), mask.MaskText(outcomeErr.Message)
	}
	return "", mask.SafeErrorText(err)
}

func fallbackAt(day time.Time, loc *time.Location, minuteOfDay int) time.Time {
	return time.Date(day.Year(), day.Month(), day.Day(), 0, 0, 0, 0, loc).
		Add(time.Duration(minuteOfDay) * time.Minute)
}

// buildRows 是 Preview 与 Build 的唯一构建函数。
func (s *Service) buildRows(ctx context.Context, in Input, eff corewindow.Window, raw coreschedule.WindowRecord) (Preview, error) {
	accounts, err := s.deps.Accounts.ActiveAccounts(ctx, in.SchoolID)
	if err != nil {
		return Preview{}, err
	}
	span := eff.End.Sub(eff.Start)
	slices := int(span / coreplan.SlotSpan)
	if slices < 1 {
		slices = 1
	}
	offsets, err := s.placeRows(ctx, in, accounts, span, slices)
	if err != nil {
		return Preview{}, err
	}
	hist := make(map[int]int, slices)
	rows := make([]Row, len(accounts))
	for i, acct := range accounts {
		off := offsets[i]
		rows[i] = Row{AccountID: acct.ID, RunAt: eff.Start.Add(off)}
		hist[coreplan.SlotOf(off)]++
	}
	capacity := corecapacity.Capacity(span, in.Workers, in.Avg, in.Rate, in.Util)
	return Preview{
		Day:         in.Day,
		Source:      contract.WindowSource(raw.Source),
		WindowStart: eff.Start,
		WindowEnd:   eff.End,
		Version:     0,
		Capacity:    capacity,
		Demand:      len(accounts),
		Shortfall:   corecapacity.Shortfall(len(accounts), capacity),
		Histogram:   hist,
		Rows:        rows,
	}, nil
}

// placeRows 先满足用户自选偏移，再把其余账号铺进剩余槽位。
// 没有偏好时，逐行落点与 core/plan.SpreadWithin 完全相同：
// 顺序模式线性填片，分层模式轮转填片。
func (s *Service) placeRows(ctx context.Context, in Input, accounts []AccountRef, span time.Duration, slices int) ([]time.Duration, error) {
	used := make([]int, slices)
	offsets := make([]time.Duration, len(accounts))
	assigned := make([]bool, len(accounts))
	if in.UsePrefs && s.deps.Prefs != nil && len(accounts) > 0 {
		if err := s.placePrefs(ctx, in, accounts, span, used, offsets, assigned); err != nil {
			return nil, err
		}
	}
	cursor := &slotCursor{used: used, cap: in.SlotsPerSlice, slices: slices, front: in.Front}
	for i := range accounts {
		if assigned[i] {
			continue
		}
		k, ok := cursor.next()
		if !ok {
			// 槽位不够：溢出压到最后一层片。片内槽号取模，
			// 与 core/plan.SpreadWithin 的夹取口径一致，行不越窗。
			k = slices - 1
			offsets[i] = sliceOffset(k, used[k]%in.SlotsPerSlice, in.SlotsPerSlice, span)
			used[k]++
			continue
		}
		offsets[i] = sliceOffset(k, used[k]%in.SlotsPerSlice, in.SlotsPerSlice, span)
		used[k]++
	}
	return offsets, nil
}

// placePrefs 按用户自选占片。偏好片满时就近顺延（同距离优先更早）。
// 偏移超出当日片数时不参与：计划不得越窗。
func (s *Service) placePrefs(ctx context.Context, in Input, accounts []AccountRef, span time.Duration, used []int, offsets []time.Duration, assigned []bool) error {
	prefs, err := s.prefsFor(ctx, accounts)
	if err != nil {
		return err
	}
	if len(prefs) == 0 {
		return nil
	}
	slices := len(used)
	for i, acct := range accounts {
		off, ok := prefs[acct.OwnerUserID]
		if !ok || off < 0 {
			continue
		}
		desired := int(time.Duration(off) * time.Minute / coreplan.SlotSpan)
		if desired >= slices {
			continue
		}
		k := coreplan.NearestFree(nil, desired, in.SlotsPerSlice, slices, func(k int) int {
			if k < 0 || k >= slices {
				return in.SlotsPerSlice
			}
			return used[k]
		})
		if k < 0 {
			continue
		}
		offsets[i] = sliceOffset(k, used[k]%in.SlotsPerSlice, in.SlotsPerSlice, span)
		used[k]++
		assigned[i] = true
	}
	return nil
}

// prefsFor 取账号所有者的自选偏移。同一所有者只查一次。
func (s *Service) prefsFor(ctx context.Context, accounts []AccountRef) (map[string]int, error) {
	seen := make(map[string]bool, len(accounts))
	ids := make([]string, 0, len(accounts))
	for _, acct := range accounts {
		id := acct.OwnerUserID
		if id == "" || seen[id] {
			continue
		}
		seen[id] = true
		ids = append(ids, id)
	}
	if len(ids) == 0 {
		return nil, nil
	}
	return s.deps.Prefs.Offsets(ctx, ids)
}

// slotCursor 在剩余槽位里按模式取下一片。
// front 从 0 号片顺序找空位；uniform 从上次片号轮转，
// 空片阵下产出 0,1,…,n-1,0,1,… 的片序（与 SpreadWithin 相同）。
type slotCursor struct {
	used   []int
	cap    int
	slices int
	front  bool
	cursor int
}

func (c *slotCursor) next() (int, bool) {
	if c.cap < 1 || c.slices < 1 {
		return 0, false
	}
	if c.front {
		for k := 0; k < c.slices; k++ {
			if c.used[k] < c.cap {
				return k, true
			}
		}
		return 0, false
	}
	for n := 0; n < c.slices; n++ {
		k := (c.cursor + n) % c.slices
		if c.used[k] < c.cap {
			c.cursor = (k + 1) % c.slices
			return k, true
		}
	}
	return 0, false
}

// sliceOffset 把片号与片内槽号换算成相对窗口起点的偏移。
// 片内按槽号等分片跨度；越界由 clampOffset 收到窗口内。
func sliceOffset(k, slot, slots int, span time.Duration) time.Duration {
	if slots < 1 {
		slots = 1
	}
	off := time.Duration(k)*coreplan.SlotSpan +
		time.Duration(slot)*coreplan.SlotSpan/time.Duration(slots)
	return clampOffset(off, span)
}

// clampOffset 把片内偏移夹进 [0, span)：留 1ns 余量保证"恒小于终点"。
// 亚毫秒窗口也成立；span ≤ 0 已由上游 Validate 拒绝。
func clampOffset(off, span time.Duration) time.Duration {
	if off < 0 {
		return 0
	}
	if off >= span {
		if span > time.Nanosecond {
			return span - time.Nanosecond
		}
		return 0
	}
	return off
}

// planRows 把预览行转为落库形状；转换只此一处。
func planRows(in Input, rows []Row) []coreschedule.PlanRow {
	out := make([]coreschedule.PlanRow, len(rows))
	for i, r := range rows {
		out[i] = coreschedule.PlanRow{
			AccountID:    r.AccountID,
			SchoolTaskID: in.SchoolTaskID,
			Day:          in.Day,
			RunAt:        r.RunAt,
		}
	}
	return out
}

func validateInput(in Input) error {
	if in.SchoolID == "" || in.SchoolTaskID == "" || in.Day == "" {
		return validation("school, task and day are required")
	}
	if in.Location == nil {
		return validation("location is required")
	}
	if in.Workers < 1 {
		return validation("workers must be at least 1")
	}
	if in.Avg <= 0 {
		return validation("avg must be positive")
	}
	if in.Rate <= 0 {
		return validation("rate must be positive")
	}
	if in.Util <= 0 {
		return validation("util must be positive")
	}
	if in.SlotsPerSlice < 1 {
		return validation("slots per slice must be at least 1")
	}
	return nil
}

func validation(message string) error {
	return coreerrs.New(coreerrs.CodeValidation, message)
}
