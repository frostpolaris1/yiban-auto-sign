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
package schedule

import (
	"context"
	"errors"
	"strings"
	"time"
	"unicode/utf8"

	appcore "github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	corecapacity "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/capacity"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreplan "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/plan"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	corewindow "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/window"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// DefaultEdge 是有效窗口两端让出的缺省缓冲：
// 防止在窗口边界发起请求。显式传 0 表示不裁剪。
const DefaultEdge = 60 * time.Second

// DefaultUtil 是容量公式的利用系数缺省值。
// 留出两成余量吸收平台抖动；与旧引擎的 0.8 同口径。
const DefaultUtil = 0.8

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
	return &Service{deps: deps}, nil
}

// Preview 只算不写：返回窗口解析结果与完整计划行。
func (s *Service) Preview(ctx context.Context, in Input) (Preview, error) {
	p, _, _, _, err := s.previewWithWindow(ctx, in)
	return p, err
}

// Build 构建计划并落库：窗口变化时同事务内写窗口、重排 pending、补插。
// 版本竞争（ErrWindowStale）时整体重试一次；仍失败即上抛。
// 事务由本方法自开：入口层不得再包一层事务（提交时点会随之改变）。
func (s *Service) Build(ctx context.Context, in Input) (Preview, error) {
	p, err := s.buildOnce(ctx, in)
	if errors.Is(err, coreschedule.ErrWindowStale) {
		// 并发写者先进：重读缓存后整体重试一次。
		p, err = s.buildOnce(ctx, in)
	}
	if err != nil {
		return Preview{}, err
	}
	return p, nil
}

// Task 按作用域读任务元数据。
// 跨校、不存在与已停用一律 404：不区分"没有"与"不给你看"。
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
func (s *Service) ListWindows(ctx context.Context, scope corescope.Scope, day string) ([]coreschedule.WindowView, error) {
	if err := requireScheduleScope(scope); err != nil {
		return nil, err
	}
	if !validDay(day) {
		return nil, validation("day must be YYYY-MM-DD")
	}
	return s.deps.Windows.ListWindows(ctx, scope, day)
}

// Override 写一条人工窗口覆盖，并按新窗口重建当日计划。
//
// 覆盖优先级最高：同任务同日存在覆盖时不再采信平台窗口。
// 覆盖写库与计划重建分两步；重建走 buildOnce，因此窗口变化检测、
// pending 重排与补插仍只有一条实现路径。
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
	p, err := s.buildOnce(ctx, in)
	if errors.Is(err, coreschedule.ErrWindowStale) {
		p, err = s.buildOnce(ctx, in)
	}
	if err != nil {
		return Preview{}, err
	}
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

// buildOnce 是一次完整尝试：预览 → 同事务写入。
func (s *Service) buildOnce(ctx context.Context, in Input) (Preview, error) {
	p, raw, changed, cachedVersion, err := s.previewWithWindow(ctx, in)
	if err != nil {
		return Preview{}, err
	}
	rows := planRows(in, p.Rows)
	err = s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		if changed {
			version, err := s.deps.Windows.PutWindow(ctx, raw, cachedVersion)
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
		return Preview{}, err
	}
	return p, nil
}

// previewWithWindow 返回预览、原始窗口、是否变化、缓存版本。
func (s *Service) previewWithWindow(ctx context.Context, in Input) (Preview, coreschedule.WindowRecord, bool, int, error) {
	if err := validateInput(in); err != nil {
		return Preview{}, coreschedule.WindowRecord{}, false, 0, err
	}
	loc := in.Location
	dayDate, err := time.ParseInLocation("2006-01-02", in.Day, loc)
	if err != nil {
		return Preview{}, coreschedule.WindowRecord{}, false, 0, validation("day must be YYYY-MM-DD")
	}
	raw, cached, cacheOK, err := s.resolveWindow(ctx, in, dayDate, loc)
	if err != nil {
		return Preview{}, coreschedule.WindowRecord{}, false, 0, err
	}
	eff, _ := corewindow.Window{Start: raw.Start, End: raw.End}.Effective(in.EdgeFront, in.EdgeBack)
	if err := corewindow.Validate(eff); err != nil {
		return Preview{}, coreschedule.WindowRecord{}, false, 0, validation("window has no positive span: " + err.Error())
	}
	cachedVersion := 0
	changed := true
	if cacheOK {
		cachedVersion = cached.Version
		changed = !cached.Start.Equal(raw.Start) || !cached.End.Equal(raw.End) ||
			cached.Source != raw.Source
	}
	p, err := s.buildRows(ctx, in, eff, raw)
	if err != nil {
		return Preview{}, coreschedule.WindowRecord{}, false, 0, err
	}
	p.Version = cachedVersion
	return p, raw, changed, cachedVersion, nil
}

// resolveWindow 按覆盖 > 平台 > 缓存 > 回退解析原始窗口。
// 平台发现失败不算错误（来源如实标注）；缓存读取失败**只有**
// errs.ErrNotFound 才算无缓存，其他错误上抛（P4-C 复审 P2-5）。
// 人工覆盖存在且合法时优先：覆盖是运维对当日窗口的明确决定。
func (s *Service) resolveWindow(ctx context.Context, in Input, day time.Time, loc *time.Location) (coreschedule.WindowRecord, coreschedule.WindowRecord, bool, error) {
	cached, cacheErr := s.deps.Windows.GetWindow(ctx, in.SchoolTaskID, in.Day)
	if cacheErr != nil && !isNotFound(cacheErr) {
		return coreschedule.WindowRecord{}, coreschedule.WindowRecord{}, false, cacheErr
	}
	cacheOK := cacheErr == nil &&
		corewindow.Validate(corewindow.Window{Start: cached.Start, End: cached.End}) == nil
	override, ovErr := s.deps.Windows.GetOverride(ctx, in.SchoolTaskID, in.Day)
	if ovErr != nil && !isNotFound(ovErr) {
		return coreschedule.WindowRecord{}, coreschedule.WindowRecord{}, false, ovErr
	}
	if ovErr == nil && corewindow.Validate(corewindow.Window{Start: override.Start, End: override.End}) == nil {
		return coreschedule.WindowRecord{
			SchoolTaskID: in.SchoolTaskID, Day: in.Day,
			Start: override.Start, End: override.End,
			Source: string(contract.WindowOverride),
		}, cached, cacheOK, nil
	}
	if platform, err := s.deps.Discover.DiscoverWindow(ctx, in.SchoolTaskID); err == nil &&
		corewindow.Validate(corewindow.Window{Start: platform.Start, End: platform.End}) == nil {
		return coreschedule.WindowRecord{
			SchoolTaskID: in.SchoolTaskID, Day: in.Day,
			Start: platform.Start, End: platform.End,
			Source: string(contract.WindowPlatform),
		}, cached, cacheOK, nil
	}
	if cacheOK {
		return cached, cached, true, nil
	}
	return coreschedule.WindowRecord{
		SchoolTaskID: in.SchoolTaskID, Day: in.Day,
		Start:  fallbackAt(day, loc, fallbackStartHM),
		End:    fallbackAt(day, loc, fallbackEndHM),
		Source: string(contract.WindowFallback),
	}, coreschedule.WindowRecord{}, false, nil
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
