package schedule

import (
	"context"
	"errors"

	appcore "github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// ErrWindowStale 是窗口版本竞争失败：库里已有更新版本。
// 哨兵定义在 core/schedule，实现与用例按同一对象判定。
var ErrWindowStale = coreschedule.ErrWindowStale

// WindowStore 读写窗口缓存、人工覆盖与任务名册。
//
// GetWindow 在无缓存时必须返回 errs.ErrNotFound；
// 其他错误（连接失败等）**原样上抛**，不得当成"无缓存"——
// 否则瞬时读错会让用例走 upsert 覆盖并发写入的窗口（P4-C 复审 P2-5）。
type WindowStore interface {
	GetWindow(ctx context.Context, schoolTaskID, day string) (coreschedule.WindowRecord, error)
	// PutWindow 写窗口。expectedVersion > 0 时做乐观锁；
	// 版本对不上返回 ErrWindowStale。
	PutWindow(ctx context.Context, w coreschedule.WindowRecord, expectedVersion int) (int, error)
	// GetOverride 返回某任务某日的人工覆盖；没有时返回 errs.ErrNotFound。
	GetOverride(ctx context.Context, schoolTaskID, day string) (coreschedule.WindowOverride, error)
	// PutOverride 覆盖写当日人工覆盖：同任务同日只保留最后一条。
	PutOverride(ctx context.Context, ov coreschedule.WindowOverride) error
	// ListWindows 返回作用域内任务与当日窗口缓存（无缓存也可有行）。
	// 顺序按学校名、任务名、任务 ID，稳定可重放。
	ListWindows(ctx context.Context, scope corescope.Scope, day string) ([]coreschedule.WindowView, error)
	// Task 读取任务元数据；不存在返回 errs.ErrNotFound。
	Task(ctx context.Context, taskID string) (coreschedule.TaskInfo, error)
}

// PlanStore 读写计划行。
type PlanStore interface {
	// InsertPlan 幂等补插：已存在的行不覆盖。返回实际写入行数。
	InsertPlan(ctx context.Context, rows []coreschedule.PlanRow) (int64, error)
	// RearrangePlan 重排未执行行：只更新 state = 'pending' 的 run_at。
	// 运行中与终态行一律不动（当日结论不可覆盖）。
	RearrangePlan(ctx context.Context, schoolTaskID, day string, rows []coreschedule.PlanRow) (int64, error)
}

// AccountRef 是铺点所需的账号最小形状：ID 加其所有者。
// 所有者用于把用户自选偏移落到账号上。
// 定义在 core/schedule，适配器不必 import app。
type AccountRef = coreschedule.AccountRef

// Accounts 提供某校的活跃账号（按稳定顺序）。
// 顺序即分层铺点的行序号，必须可重放。
type Accounts interface {
	ActiveAccounts(ctx context.Context, schoolID string) ([]AccountRef, error)
}

// TimePrefs 读取用户自选偏移（相对有效窗口起点的分钟数）。
// 只返回有偏好且取值合法的用户；无偏好不出现。
type TimePrefs interface {
	Offsets(ctx context.Context, userIDs []string) (map[string]int, error)
}

// Discoverer 发现平台窗口。实现复用 signing provider。
type Discoverer interface {
	DiscoverWindow(ctx context.Context, schoolTaskID string) (contract.Window, error)
}

// Deps 是计划用例的依赖。
// Tx 让"写窗口＋重排＋补插"三步同事务：任一步失败整体回滚，
// 不留下"版本已推进但计划未重排"的半成品（P4-C 复审 P2-4）。
// Prefs 可为 nil：未装配时计划忽略用户自选，行为等于纯自动铺点。
type Deps struct {
	Windows  WindowStore
	Plans    PlanStore
	Accounts Accounts
	Discover Discoverer
	Prefs    TimePrefs
	Tx       appcore.TxManager
}

// isNotFound 是"无缓存"的判据：实现必须用 errs.ErrNotFound 表达。
func isNotFound(err error) bool {
	return err != nil && errors.Is(err, coreerrs.ErrNotFound)
}
