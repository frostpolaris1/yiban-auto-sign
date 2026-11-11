package schedule

import (
	"context"
	"errors"
	"log/slog"

	appcore "github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
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
// 否则瞬时读错会让用例走 upsert 覆盖并发写入的窗口。
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

// SelfReader 读用户自选读面所需的账号与任务上下文（实现为 pg.ScheduleRepo）。
// ActiveAccountByOwner 在用户没有生效账号时返回 errs.ErrNotFound。
// DayTaskRows 只返回启用任务的行，按"未了结优先、run_at、任务名、任务 ID"排序：
// 第一行就是该账号当日最先要执行的任务。
// AccountEnabledTasks 只返回启用任务，按任务名、任务 ID 排序。
type SelfReader interface {
	ActiveAccountByOwner(ctx context.Context, userID string) (coreschedule.SelfAccount, error)
	DayTaskRows(ctx context.Context, accountID, day string) ([]coreschedule.SelfTask, error)
	AccountEnabledTasks(ctx context.Context, accountID string) ([]string, error)
}

// CapacityAlerts 是容量缺口通知的投递端口（实现为 app/notify.Dispatcher）。
// 为 nil 时不投递，现行为不变；失败或 panic 只记日志，不改业务返回。
// 事件类型来自 core/notify：适配器因此无须导入 app 层。
type CapacityAlerts interface {
	Dispatch(ctx context.Context, event corenotify.Event) error
}

// DefaultSignWindow 解析全局默认签到窗口对（批 3b）：
// 返回业务日内的分钟值（start<end）。实现是 app/settings.Resolver 的
// ResolveSignWindow（DB 行>缺省常量，半对整对回退）。
// 为 nil 时直接用 core/schedule 常量对，行为与既有完全一致。
// 返回错误时本次窗口解析失败上抛：故障不是配置状态。
// 装配注入的是**同一闭包**，同时喂计划 fallback 与 timepref 自选展示窗
// （3b 复审 F1：默认窗可写回后两处必须同源，
// 否则 GET /api/me/time-pref 显示窗与铺行窗漂移成第二事实源）。
type DefaultSignWindow func(ctx context.Context) (startMin, endMin int, err error)

// Deps 是计划用例的依赖。
// Tx 让"写窗口＋重排＋补插"三步同事务：任一步失败整体回滚，
// 不留下"版本已推进但计划未重排"的半成品。
// Prefs 可为 nil：未装配时计划忽略用户自选，行为等于纯自动铺点。
// Self 可为 nil：未装配时 SelfContext 返回零值，用户自选读面保持标准窗口。
type Deps struct {
	Windows  WindowStore
	Plans    PlanStore
	Accounts Accounts
	Discover Discoverer
	Prefs    TimePrefs
	Self     SelfReader
	Tx       appcore.TxManager
	// Notifier 可为 nil：缺端口就不发事件。
	Notifier Notifier
	// CapacityAlerts 可为 nil：缺端口就不投递容量缺口通知。
	CapacityAlerts CapacityAlerts
	// Defaults 解析全局默认签到窗口对（批 3b）。为 nil 时用
	// core/schedule 常量对（fallback 窗口与用户自选读面的展示窗），
	// 行为与既有完全一致。
	Defaults DefaultSignWindow
	// Paused 查询全局急停。显式手动面（Input.Explicit）在写库前先判：
	// 暂停时零写入并回 PausedError（入口 503）。为 nil 时不判。
	// 日切/自动路径不读它：暂停由发现错误回退处理（见 resolveWindow）。
	Paused func(ctx context.Context) (bool, error)
	// Logger 可为 nil：用 slog.Default。窗口发现失败的原因进日志。
	Logger *slog.Logger
}

// isNotFound 是"无缓存"的判据：实现必须用 errs.ErrNotFound 表达。
func isNotFound(err error) bool {
	return err != nil && errors.Is(err, coreerrs.ErrNotFound)
}
