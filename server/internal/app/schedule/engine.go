package schedule

// 执行监控快照用例：单进程执行面的只读聚合（UX-5）。
//
// # 功能
// Snapshot 按作用域返回一次快照：在飞行、到期计数、当日终态计数、
// 待重试行。本文件**只读**：不改任何行，不触发窗口发现。
//
// # 归属
// app/schedule 用例层。端口 EngineSource 实现为 pg.ScheduleRepo；
// 入口层是 entry/http/schedule 的 EngineHandler。
//
// # 口径
// 作用域只收 master 与 admin：user 范围没有执行可见面，直接拒绝，
// 不把空集合当「自己的执行面」。admin 空绑定就是空快照，不是全量。
// 业务日与时钟由装配注入：day 取注入时区的今天，now 取注入时钟。
// **手机号只出掩码**：行内明文在映射成视图时经 core/mask.MaskPhone，
// result 经 core/mask.MaskText；明文不出本包。
// 进程内三时刻不在本用例：那是 Loop 的 EngineState，入口层另行注入。

import (
	"context"
	"time"

	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coremask "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// EngineSource 是执行监控快照的只读端口（实现为 *pg.ScheduleRepo）。
// 返回的行含明文手机号：调用方只允许是用例层，出口前必须掩码。
type EngineSource interface {
	EngineSnapshot(ctx context.Context, scope corescope.Scope, day string, now time.Time) (coreschedule.EngineData, error)
}

// EngineInflight 是给入口层的在飞行：手机号已掩码。
type EngineInflight struct {
	AccountID      string
	PhoneMasked    string
	SchoolID       string
	SchoolName     string
	ClaimedAt      time.Time
	LeaseExpiresAt time.Time
}

// EngineRetry 是给入口层的待重试行：手机号已掩码，result 已过自由文本掩码。
type EngineRetry struct {
	AccountID   string
	PhoneMasked string
	SchoolID    string
	SchoolName  string
	Attempts    int
	RunAt       time.Time
	Result      string
}

// EngineSnapshot 是一次执行监控快照的视图。
type EngineSnapshot struct {
	Inflight     []EngineInflight
	DueNow       int64
	TodaySuccess int64
	TodayFailed  int64
	Retrying     []EngineRetry
}

// EngineQuery 是执行监控快照用例。
type EngineQuery struct {
	src   EngineSource
	clock coreclock.Clock
	loc   *time.Location
}

// NewEngineQuery 构造用例。Clock 为空时用系统墙钟；Location 为空时按 UTC。
func NewEngineQuery(src EngineSource, clk coreclock.Clock, loc *time.Location) *EngineQuery {
	if clk == nil {
		clk = coreclock.System{}
	}
	if loc == nil {
		loc = time.UTC
	}
	return &EngineQuery{src: src, clock: clk, loc: loc}
}

// Snapshot 返回作用域内的执行监控快照。
// 业务日按注入时区的今天取：当日终态计数的「当日」只有这一份口径。
func (q *EngineQuery) Snapshot(ctx context.Context, scope corescope.Scope) (EngineSnapshot, error) {
	switch scope.Kind() {
	case corescope.KindMaster, corescope.KindAdmin:
	default:
		return EngineSnapshot{}, coreerrs.New(coreerrs.CodeForbidden, "role cannot read the engine")
	}
	now := q.clock.Now()
	data, err := q.src.EngineSnapshot(ctx, scope, now.In(q.loc).Format("2006-01-02"), now)
	if err != nil {
		return EngineSnapshot{}, err
	}
	return engineView(data), nil
}

// engineView 把仓储行映射成视图并掩码。明文不出本函数。
func engineView(data coreschedule.EngineData) EngineSnapshot {
	inflight := make([]EngineInflight, len(data.Inflight))
	for i, r := range data.Inflight {
		inflight[i] = EngineInflight{
			AccountID:      r.AccountID,
			PhoneMasked:    coremask.MaskPhone(r.Phone),
			SchoolID:       r.SchoolID,
			SchoolName:     r.SchoolName,
			ClaimedAt:      r.ClaimedAt,
			LeaseExpiresAt: r.LeaseExpiresAt,
		}
	}
	retrying := make([]EngineRetry, len(data.Retrying))
	for i, r := range data.Retrying {
		retrying[i] = EngineRetry{
			AccountID:   r.AccountID,
			PhoneMasked: coremask.MaskPhone(r.Phone),
			SchoolID:    r.SchoolID,
			SchoolName:  r.SchoolName,
			Attempts:    r.Attempts,
			RunAt:       r.RunAt,
			Result:      coremask.MaskText(r.Result),
		}
	}
	return EngineSnapshot{
		Inflight:     inflight,
		DueNow:       data.DueNow,
		TodaySuccess: data.TodaySuccess,
		TodayFailed:  data.TodayFailed,
		Retrying:     retrying,
	}
}
