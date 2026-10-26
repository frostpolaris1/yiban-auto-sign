// Package schedule 实现计划构建与预览用例（P4-C）。
//
// # 功能
// 解析当日窗口（平台 > 缓存 > 回退），构建账号落点计划，
// 幂等落库并在窗口版本变化时重排未执行的计划行。
// Preview 与 Build 共用同一个构建函数：预览就是计划本身，不是另一套估算。
// 本包**不**发 HTTP 请求、**不**写 SQL、**不**实现入口端点。
//
// # 归属
// app 层用例。端口见 repo.go，实现见 adapters/pg。
// 入口层在 P4-F 接线；窗口发现复用 signing 契约。
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
	"fmt"
	"time"

	corecapacity "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/capacity"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreplan "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/plan"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corewindow "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/window"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// DefaultEdge 是有效窗口两端让出的缺省缓冲：
// 防止在窗口边界发起请求。显式传 0 表示不裁剪。
const DefaultEdge = 60 * time.Second

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
	Version     int
	Capacity    int
	Demand      int
	Shortfall   int
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
func (s *Service) Build(ctx context.Context, in Input) (Preview, error) {
	for attempt := 0; attempt < 2; attempt++ {
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
		if errors.Is(err, coreschedule.ErrWindowStale) && attempt == 0 {
			continue // 并发写者先进：重读缓存后整体重试一次。
		}
		if err != nil {
			return Preview{}, err
		}
		return p, nil
	}
	return Preview{}, fmt.Errorf("schedule: window changed concurrently twice")
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

// resolveWindow 按平台 > 缓存 > 回退解析原始窗口。
// 平台发现失败不算错误（来源如实标注）；缓存读取失败**只有**
// errs.ErrNotFound 才算无缓存，其他错误上抛（P4-C 复审 P2-5）。
func (s *Service) resolveWindow(ctx context.Context, in Input, day time.Time, loc *time.Location) (coreschedule.WindowRecord, coreschedule.WindowRecord, bool, error) {
	cached, cacheErr := s.deps.Windows.GetWindow(ctx, in.SchoolTaskID, in.Day)
	if cacheErr != nil && !isNotFound(cacheErr) {
		return coreschedule.WindowRecord{}, coreschedule.WindowRecord{}, false, cacheErr
	}
	cacheOK := cacheErr == nil &&
		corewindow.Validate(corewindow.Window{Start: cached.Start, End: cached.End}) == nil
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
	accounts, err := s.deps.Accounts.ActiveAccountIDs(ctx, in.SchoolID)
	if err != nil {
		return Preview{}, err
	}
	span := eff.End.Sub(eff.Start)
	slices := int(span / coreplan.SlotSpan)
	if slices < 1 {
		slices = 1
	}
	offsets := coreplan.SpreadWithin(len(accounts), in.SlotsPerSlice, slices, in.Front)
	hist := make(map[int]int, slices)
	rows := make([]Row, len(accounts))
	for i, acct := range accounts {
		off := clampOffset(offsets[i], span)
		rows[i] = Row{AccountID: acct, RunAt: eff.Start.Add(off)}
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
