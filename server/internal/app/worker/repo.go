// Package worker 实现签到执行循环（P4-E）。
//
// # 功能
// 认领到期任务、按窗口与限速执行、按结论落终态或退避重排；
// 启动时回收上次崩溃遗留的 running 行；提供手动签到的入队入口。
// 本包**不**直接发 HTTP 请求（经 signing 契约）、**不**写 SQL。
//
// # 归属
// app 层用例。端口见本文件，存储实现见 adapters/pg，执行实现见 signing。
//
// # 复用
// Service、Deps、Request；端口 Store、Credentials、Signer、Egress、Limiter。
//
// # 口径
// 每账号串行：认领 SQL 排除已有 running 的账号，库层唯一索引是最终兜底。
// 窗口由 core/window 判定：关窗落 skipped(window_closed)，提前到点退回等待。
// 重试预算与落点来自 core/retry：不越窗，尾段放不下即失败。
// 状态词表只认 core/status；结论词表只认 signing/contract。
package worker

import (
	"context"
	"time"

	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreegress "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/egress"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// Store 是任务队列的存储端口。
type Store interface {
	// ClaimDue 认领到期任务并推进到 running（单语句 CAS）。
	ClaimDue(ctx context.Context, now time.Time, limit int) ([]coreschedule.TaskRow, error)
	// Finish 把 running 行落终态。
	Finish(ctx context.Context, accountID, schoolTaskID, day string, state corestatus.TaskState, result string, now time.Time) error
	// RequeueWait 是"等待型"重排：窗口未开、限速、出口不可用、
	// 批处理中止。**不计业务尝试数**——等待不是一次失败的尝试。
	RequeueWait(ctx context.Context, taskID string, runAt time.Time, result string) error
	// RequeueRetry 是业务失败后的退避：计一次尝试数。
	RequeueRetry(ctx context.Context, taskID string, runAt time.Time, result string) error
	// Rearm 把当日 failed 行放回队列（手动签到的重试入口）。
	Rearm(ctx context.Context, taskID string, runAt time.Time) error
	// ResetRunning 回收孤儿 running 行（启动时调用）。
	ResetRunning(ctx context.Context) (int64, error)
	// GetWindow 取任务当日窗口；无缓存返回 errs.ErrNotFound。
	GetWindow(ctx context.Context, schoolTaskID, day string) (coreschedule.WindowRecord, error)
	// EnsureTask 为手动签到建或取当日任务行；created 表示本次插入。
	EnsureTask(ctx context.Context, accountID, schoolTaskID, day string, runAt time.Time) (coreschedule.TaskRow, bool, error)
	// TodayTask 返回账号所属学校今天启用的第一个任务。
	TodayTask(ctx context.Context, accountID string) (string, error)
}

// Credentials 取一次调用内有效的账号凭据。
type Credentials interface {
	Credential(ctx context.Context, accountID string) (contract.Credential, error)
}

// Signer 是 signing 契约的执行子集。
type Signer interface {
	DiscoverTargets(ctx context.Context, req contract.DiscoverRequest) ([]contract.Target, error)
	Sign(ctx context.Context, req contract.SignRequest) (contract.Result, error)
}

// Egress 返回账号的粘性出口。
type Egress interface {
	Assign(ctx context.Context, accountID string) (coreegress.Record, error)
}

// RateGate 是每出口 GCRA 的取额接口（实现为 core/rate.Limiter）。
type RateGate interface {
	Admit(egress string, now time.Time) bool
	RetryAfter(egress string, now time.Time) time.Duration
}

// Deps 是执行循环的依赖。
type Deps struct {
	Store    Store
	Creds    Credentials
	Signer   Signer
	Egress   Egress
	Limiter  RateGate
	Clock    coreclock.Clock
	Location *time.Location
}
