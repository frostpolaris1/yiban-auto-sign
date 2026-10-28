// Package timepref 实现用户自选时间片用例（P4-F）。
//
// # 功能
// 读、写、清除当前用户的自选偏移；执行部署开关、粒度与冷却判据。
// 偏移是相对有效窗口起点的分钟数，按 5 分钟粒度存储。
// 本包**不**查计划、**不**算窗口、**不**发 HTTP 请求。
//
// # 归属
// app 层用例。端口见本文件，adapters/pg 实现；
// 时间片如何影响落点在 app/schedule。
//
// # 复用
// Service、View、Store、Deps；MaxOffsetMin、GranularityMin。
//
// # 口径
// 关闭开关时不读、不写：已存值可读但计划忽略它。
// 冷却只按上一次写入时间判定，进程重启不丢。
package timepref

import (
	"context"
	"errors"
	"time"

	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
)

const (
	// MaxOffsetMin 是偏移上限（不含）：业务日最多 1440 分钟。
	MaxOffsetMin = 1440
	// GranularityMin 是偏移粒度，与计划片跨度一致。
	GranularityMin = 5
)

// Store 读写用户自选。
// Get 在无记录时必须返回 errs.ErrNotFound；其他错误原样上抛。
// Store 是自选持久化端口。
// **冷却判定在存储侧一次完成**（条件 UPSERT＋受影响行数）：先读后写会在两次
// 并发写之间留下竞态窗口（k0y-3）。updated=false 表示冷却未过、未写入。
type Store interface {
	Get(ctx context.Context, userID string) (coreschedule.TimePref, error)
	PutWithin(ctx context.Context, userID string, offset *int, now time.Time, cooldown time.Duration) (bool, error)
}

// Deps 是用例依赖。
// Allow 是部署开关 YIBAN_ALLOW_TIME_PREF。
// Cooldown 是两次写入之间的最小间隔；非正值表示不限制。
type Deps struct {
	Store    Store
	Clock    coreclock.Clock
	Allow    bool
	Cooldown time.Duration
}

// View 是对外读数。
type View struct {
	OffsetMin *int
	Allowed   bool
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
	return &Service{deps: deps}, nil
}

// Get 返回用户当前自选。没有记录时 OffsetMin 为 nil。
// 关闭开关时仍返回已存值：界面要显示"功能未开启"，而不是假装未设置。
func (s *Service) Get(ctx context.Context, userID string) (View, error) {
	if userID == "" {
		return View{}, coreerrs.New(coreerrs.CodeInternal, "time preference requires a user")
	}
	pref, err := s.deps.Store.Get(ctx, userID)
	if err != nil {
		if errors.Is(err, coreerrs.ErrNotFound) {
			return View{Allowed: s.deps.Allow}, nil
		}
		return View{}, err
	}
	return View{OffsetMin: pref.OffsetMin, Allowed: s.deps.Allow}, nil
}

// Set 写或清除自选。
// 关闭开关时拒绝写入；冷却期内拒绝切换；粒度与范围在写前校验。
// 冷却与写入是同一条语句（PutWithin）：并发两次 PUT 只有一个能落。
func (s *Service) Set(ctx context.Context, userID string, offset *int) (View, error) {
	if userID == "" {
		return View{}, coreerrs.New(coreerrs.CodeInternal, "time preference requires a user")
	}
	if !s.deps.Allow {
		return View{}, coreerrs.New(coreerrs.CodeForbidden, "time preference is disabled")
	}
	if offset != nil {
		if *offset < 0 || *offset >= MaxOffsetMin || *offset%GranularityMin != 0 {
			return View{}, coreerrs.New(coreerrs.CodeValidation,
				"offset must be a multiple of 5 between 0 and 1439")
		}
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
	return View{OffsetMin: offset, Allowed: true}, nil
}
