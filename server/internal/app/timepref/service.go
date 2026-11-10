// Package timepref 实现用户自选时间片用例（P4-F）。
//
// # 功能
// 读、写、清除当前用户的自选偏移；执行部署开关、粒度与冷却判据。
// 偏移是相对有效窗口起点的分钟数，按 5 分钟粒度存储。
// 读面同时给出服务端默认签到窗口与 5 分钟锚点，供界面回显。
// 本包**不**查计划、**不**读平台窗口、**不**发 HTTP 请求。
//
// # 归属
// app 层用例。端口见本文件，adapters/pg 实现；
// 时间片如何影响落点在 app/schedule。
//
// # 复用
// Service、View、Store、Deps。取值边界取自 core/schedule
// （MaxOffsetMin、GranularityMin、DefaultSignWindow*）；建表 CHECK（00014）取同值。
//
// # 口径
// 关闭开关时不读、不写：已存值可读但计划忽略它。
// 冷却只按上一次写入时间判定，进程重启不丢。
// 窗口值只取 core/schedule 的默认常量：与计划回退窗口同源，不复制第二份。
package timepref

import (
	"context"
	"errors"
	"fmt"
	"time"

	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
)

// Store 读写用户自选。
// Get 在无记录时必须返回 errs.ErrNotFound；其他错误原样上抛。
// Store 是自选持久化端口。
// **冷却判定在存储侧一次完成**（条件 UPSERT＋受影响行数）：先读后写会在两次
// 并发写之间留下竞态窗口（k0y-3，并发面由 pg 真库用例钉住）。
// updated=false 表示冷却未过、未写入。
type Store interface {
	Get(ctx context.Context, userID string) (coreschedule.TimePref, error)
	PutWithin(ctx context.Context, userID string, offset *int, now time.Time, cooldown time.Duration) (bool, error)
}

// Deps 是用例依赖。
// Allow 是部署开关（YIBAN_ALLOW_TIME_PREF）的每请求解析：
// settings 写回后 DB 覆盖 env（sett2），故取函数而非定值。
// 返回错误原样上抛——故障不是「关闭」；装配未接写回层时
// 传「恒返 env 值」的闭包即可，行为与定值完全一致。
// Window 是标准窗口的每请求解析对（分钟）：计划回退窗与自选展示窗
// **必须同源**（3b 复审 F1——默认窗已可写回，裸常量会漂移成第二事实源）。
// 返回错误原样上抛，口径同 Allow；nil 仅用于未接写回层的装配，回退 core 常量对。
// Cooldown 是两次写入之间的最小间隔；非正值表示不限制。
type Deps struct {
	Store    Store
	Clock    coreclock.Clock
	Allow    func(ctx context.Context) (bool, error)
	Window   func(ctx context.Context) (startMin, endMin int, err error)
	Cooldown time.Duration
}

// Window 是服务端标准签到窗口的展示值，HH:MM。
// 它不是执行承诺：实际窗口可能被平台发现窗口或逐任务 override 平移。
type Window struct {
	Start string
	End   string
}

// Slot 是标准窗口内的一个 5 分钟锚点。
// OffsetMin 相对标准窗口起点；Label 是该窗口内的时钟时刻，不是实际执行时刻。
// Disabled 为真时 Reason 非空。
type Slot struct {
	OffsetMin int
	Label     string
	Disabled  bool
	Reason    string
}

// View 是对外读数。
type View struct {
	OffsetMin *int
	Allowed   bool
	Window    Window
	Slots     []Slot
}

// Service 是自选用例服务。
type Service struct {
	deps Deps
}

// NewService 构造服务。缺依赖即失败。
func NewService(deps Deps) (*Service, error) {
	if deps.Store == nil {
		return nil, errors.New("timepref: nil store")
	}
	if deps.Clock == nil {
		return nil, errors.New("timepref: nil clock")
	}
	if deps.Allow == nil {
		return nil, errors.New("timepref: nil allow resolver")
	}
	return &Service{deps: deps}, nil
}

// allowed 每请求解析部署开关。解析失败原样上抛：
// 关闭是业务状态，故障是故障，不得混读。
func (s *Service) allowed(ctx context.Context) (bool, error) {
	return s.deps.Allow(ctx)
}

// window 每请求解析标准窗口对，与计划 fallback 同源（F1）。
// nil 端口=未接写回层的装配，取 core 常量对——与 schedule
// defaultWindow 的 nil 语义逐字一致。
func (s *Service) window(ctx context.Context) (int, int, error) {
	if s.deps.Window == nil {
		return coreschedule.DefaultSignWindowStartMinute, coreschedule.DefaultSignWindowEndMinute, nil
	}
	return s.deps.Window(ctx)
}

// Get 返回用户当前自选。没有记录时 OffsetMin 为 nil。
// 关闭开关时仍返回已存值：界面要显示"功能未开启"，而不是假装未设置。
func (s *Service) Get(ctx context.Context, userID string) (View, error) {
	if userID == "" {
		return View{}, coreerrs.New(coreerrs.CodeInternal, "time preference requires a user")
	}
	allow, err := s.allowed(ctx)
	if err != nil {
		return View{}, err
	}
	startMin, endMin, err := s.window(ctx)
	if err != nil {
		return View{}, err
	}
	pref, err := s.deps.Store.Get(ctx, userID)
	if err != nil {
		if errors.Is(err, coreerrs.ErrNotFound) {
			return s.view(nil, allow, startMin, endMin), nil
		}
		return View{}, err
	}
	return s.view(pref.OffsetMin, allow, startMin, endMin), nil
}

// Set 写或清除自选。
// 关闭开关时拒绝写入；冷却期内拒绝切换；粒度与范围在写前校验。
// 冷却与写入是同一条语句（PutWithin）：并发两次 PUT 只有一个能落。
func (s *Service) Set(ctx context.Context, userID string, offset *int) (View, error) {
	if userID == "" {
		return View{}, coreerrs.New(coreerrs.CodeInternal, "time preference requires a user")
	}
	allow, err := s.allowed(ctx)
	if err != nil {
		return View{}, err
	}
	if !allow {
		return View{}, coreerrs.New(coreerrs.CodeForbidden, "time preference is disabled")
	}
	if offset != nil {
		if *offset < 0 || *offset >= coreschedule.MaxOffsetMin || *offset%coreschedule.GranularityMin != 0 {
			return View{}, coreerrs.New(coreerrs.CodeValidation,
				"offset must be a multiple of 5 between 0 and 1439")
		}
	}
	startMin, endMin, err := s.window(ctx)
	if err != nil {
		return View{}, err
	}
	now := s.deps.Clock.Now()
	updated, err := s.deps.Store.PutWithin(ctx, userID, offset, now, s.deps.Cooldown)
	if err != nil {
		return View{}, err
	}
	if !updated {
		return View{}, coreerrs.New(coreerrs.CodeRateLimited,
			"time preference changed too recently")
	}
	return s.view(offset, allow, startMin, endMin), nil
}

// view 组装读面。窗口对来自每请求解析（s.window），与计划回退窗同源：
// 默认窗写回后（3b），自选展示窗口不会与铺行窗口出现两套值。
// 这里是服务端标准窗口；实际执行窗口可被平台窗口或 override 平移，
// slot label 只表示相对标准窗口的偏好时刻。
func (s *Service) view(offset *int, allow bool, start, end int) View {
	out := View{
		OffsetMin: offset,
		Allowed:   allow,
		Window:    Window{Start: clockLabel(start), End: clockLabel(end)},
	}
	width := end - start
	if width <= 0 {
		return out
	}
	slots := width / coreschedule.GranularityMin
	limit := coreschedule.MaxOffsetMin / coreschedule.GranularityMin
	if slots > limit {
		slots = limit
	}
	reason := ""
	if !allow {
		// 关闭开关时槽位仍列出，但全部不可选：界面显示原因而不是空白。
		reason = "time preference is disabled"
	}
	out.Slots = make([]Slot, 0, slots)
	for i := 0; i < slots; i++ {
		offsetMin := i * coreschedule.GranularityMin
		out.Slots = append(out.Slots, Slot{
			OffsetMin: offsetMin,
			Label:     clockLabel(start + offsetMin),
			Disabled:  !allow,
			Reason:    reason,
		})
	}
	return out
}

// clockLabel 把当日分钟数格式化成 HH:MM。
func clockLabel(minute int) string {
	return fmt.Sprintf("%02d:%02d", minute/60, minute%60)
}
