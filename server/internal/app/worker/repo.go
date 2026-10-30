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
	"log/slog"
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
	// 租约由仓储写入 lease_expires_at；过期的 running 由 ReclaimExpired 回收。
	ClaimDue(ctx context.Context, now time.Time, limit int) ([]coreschedule.TaskRow, error)
	// ReclaimExpired 把租约过期的 running 行放回 pending，并写 task.reclaim 审计。
	ReclaimExpired(ctx context.Context, now time.Time, limit int) (int64, error)
	// Finish 把 running 行落终态。
	Finish(ctx context.Context, accountID, schoolTaskID, day string, state corestatus.TaskState, result string, claimSeq int, now time.Time) error
	// FinishCredentialInvalid 把 running 行落 failed(credential_invalid)，
	// 并在同一事务里把 active 账号改为 credential_error 且写引擎审计。
	// 账号已非 active 时保持原状：不改状态，也不写审计。
	FinishCredentialInvalid(ctx context.Context, schoolID, accountID, schoolTaskID, day string, claimSeq int, now time.Time) error
	// RequeueWait 是"等待型"重排：窗口未开、限速、出口不可用、
	// 批处理中止。**不计业务尝试数**——等待不是一次失败的尝试。
	RequeueWait(ctx context.Context, taskID string, runAt time.Time, result string, claimSeq int) error
	// RequeueRetry 是业务失败后的退避：计一次尝试数。
	RequeueRetry(ctx context.Context, taskID string, runAt time.Time, result string, claimSeq int) error
	// Rearm 把当日 failed 行放回队列（手动签到的重试入口）。
	Rearm(ctx context.Context, taskID string, runAt time.Time) error
	// ResetRunning 回收孤儿 running 行（启动时调用）。
	// 仓储带 advisory lock：多实例同时启动时只有一个执行。
	ResetRunning(ctx context.Context) (int64, error)
	// CountRunning 数当前 running 行。优雅停机用它报在飞清零。
	CountRunning(ctx context.Context) (int64, error)
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

// LocationSource 解析账号的定位策略（实现为 app/location.Service）。
// 为 nil 时 SignRequest 恒用 LocationAuto：未装配定位面就保持现行为。
type LocationSource interface {
	Resolve(ctx context.Context, accountID string) (contract.Location, error)
}

// PhotoSource 为一次尝试选一张图片（实现为 app/photos.Service）。
// ownerUserID 来自任务行的账号属主；attemptID 决定确定性的选图轮换。
// 为 nil 时等同空图库：要求图片的目标按 photo_missing 处理（fail-closed）。
type PhotoSource interface {
	PickForAttempt(ctx context.Context, ownerUserID, attemptID string) (contract.ImageRef, bool, error)
}

// Notifier 是任务事件的发布端口（实现为 app/events.Broker 的适配器）。
// kind 取 app/events 的 Type 词表。为 nil 时不发布，现行为不变。
// 实现必须不阻塞；实现 panic 视为发布失败，只丢事件（见 notify）。
type Notifier interface {
	TaskEvent(ctx context.Context, schoolID, taskID, day string, kind string, payload map[string]any)
}

// Deps 是执行循环的依赖。Locations 与 Photos 可为 nil：
// 缺哪个端口就退回哪个面的现行为（LocationAuto、无图注入）。
// Notifier 可为 nil：缺端口就不发事件。
// Logger 可为 nil：用 slog.Default。结论原因进日志，见 logOutcome。
type Deps struct {
	Store     Store
	Creds     Credentials
	Signer    Signer
	Egress    Egress
	Limiter   RateGate
	Clock     coreclock.Clock
	Location  *time.Location
	Locations LocationSource
	Photos    PhotoSource
	Notifier  Notifier
	Logger    *slog.Logger
}
