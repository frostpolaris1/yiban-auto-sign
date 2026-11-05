// Package logfield 是结构化日志字段键的唯一表。
//
// # 功能
// 列出全部日志字段键及其语义类别，供调用点引用常量而不是裸字符串。
// 本包**不**写日志、**不**格式化值、**不**改变消息文本或输出格式。
//
// # 归属
// core 层纯领域包。任何写结构化日志的包都从这里取键。
// 键的增删以实际调用点为准：新键先在调用点出现，再来本表登记；
// 删调用点时一并删掉无人引用的键。
//
// # 复用
// Key* 常量；Field、Kind、All。
//
// # 口径
// 命名规则（与既有日志字段一致）：
//   - 标识符用 `<noun>_id`，如 account_id、school_task_id；
//   - 日期用 `*_day`，值是业务日 YYYY-MM-DD；
//   - 时长用 `duration_ms`，值是整数毫秒；配置时长串用 `*_interval`，如 poll_interval；
//   - 计数用过去分词复数或复数名词（rows_deleted、sessions），
//     也允许裸计量词（count、remaining、batch、attempt、built）；
//   - 布尔用形容词，如 retryable、history_available；
//   - 错误用 `error`；
//   - 枚举/词表裸写，如 outcome、reason、stage、kind、event、suggestion。
//
// 键是日志读者与检索规则的公开接口：改键名等于改可观测性契约，
// 只允许在确认无消费方后整体迁移。
package logfield

import corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"

// Kind 是字段值的语义类别，供读者与审查判断该键该装什么。
type Kind string

const (
	// KindID 是实体标识符，值一般是库表主键或等价指纹。
	KindID Kind = "id"
	// KindDay 是业务日期串（YYYY-MM-DD），不是时刻。
	KindDay Kind = "day"
	// KindDuration 是时长，值固定为整数毫秒。
	KindDuration Kind = "duration"
	// KindCount 是计数，值是非负整数。
	KindCount Kind = "count"
	// KindBool 是布尔判定。
	KindBool Kind = "bool"
	// KindError 是错误详情；写入前必须已按 mask 口径脱敏。
	KindError Kind = "error"
	// KindEnum 是封闭词表取值，不得临时造词。
	KindEnum Kind = "enum"
	// KindNumber 是数值状态码或编号，不是计数。
	KindNumber Kind = "number"
	// KindText 是其余自由文本或标量（主机名、路径、掩码邮箱等）。
	KindText Kind = "text"
	// KindMap 是映射型值：键按 contract.Outcome 词表，值是计数。
	// 未识别值原样保留，漂移可见；用于把一组分布压进一个字段（如 outcome_counts）。
	KindMap Kind = "map"
)

// Field 是日志字段键的一条登记。
type Field struct {
	// Key 是实际写进日志的键串。
	Key string
	// Const 是导出常量名，与 Key 一一对应。
	Const string
	// Kind 是值的语义类别。
	Kind Kind
	// Meaning 是一句中文语义，说明这个键装什么。
	Meaning string
}

// 日志字段键。新增键时必须同时登记到 All。
const (
	KeyAccountID        = "account_id"
	KeySchoolID         = "school_id"
	KeySchoolTaskID     = "school_task_id"
	KeyTaskID           = "task_id"
	KeyUserID           = "user_id"
	KeyRequestID        = "request_id"
	KeyDay              = "day"
	KeyLastSuccessDay   = "last_success_day"
	KeyDurationMS       = "duration_ms"
	KeyCount            = "count"
	KeyRemaining        = "remaining"
	KeyBatch            = "batch"
	KeyAttempt          = "attempt"
	KeyFailedDays       = "failed_days"
	KeyRowsDeleted      = "rows_deleted"
	KeyBlobsDeleted     = "blobs_deleted"
	KeySessions         = "sessions"
	KeyResetTokens      = "reset_tokens"
	KeyTasks            = "tasks"
	KeyNotifications    = "notifications"
	KeyBuilt            = "built"
	KeyBytes            = "bytes"
	KeyTotal            = "total"
	KeySuccess          = string(corestatus.TaskSuccess)
	KeyFailed           = string(corestatus.TaskFailed)
	KeySkipped          = string(corestatus.TaskSkipped)
	KeyPending          = string(corestatus.TaskPending)
	KeyStatus           = "status"
	KeyPort             = "port"
	KeyPollInterval     = "poll_interval"
	KeyHost             = "host"
	KeyTLSMode          = "tls_mode"
	KeyMethod           = "method"
	KeyPath             = "path"
	KeyTrigger          = "trigger"
	KeyStage            = "stage"
	KeyOutcome          = "outcome"
	KeyOutcomeCounts    = "outcome_counts"
	KeyReason           = "reason"
	KeySuggestion       = "suggestion"
	KeyCredentialKID    = "credential_kid"
	KeyHistoryAvailable = "history_available"
	KeyRetryable        = "retryable"
	KeyError            = "error"
	KeyEvent            = "event"
	KeyEmail            = "email"
	KeyKind             = "kind"
	KeyPanic            = "panic"
)

// All 是全部日志字段键的登记表。
// 键集合与实际调用点一致：多登记一条就是一条无人使用的契约。
var All = []Field{
	{Key: KeyAccountID, Const: "KeyAccountID", Kind: KindID, Meaning: "账号标识（accounts.id）"},
	{Key: KeySchoolID, Const: "KeySchoolID", Kind: KindID, Meaning: "学校标识（schools.id）"},
	{Key: KeySchoolTaskID, Const: "KeySchoolTaskID", Kind: KindID, Meaning: "学校任务标识（school_tasks.id）"},
	{Key: KeyTaskID, Const: "KeyTaskID", Kind: KindID, Meaning: "任务标识；当前调用点写入 school_tasks.id（非 sign_tasks.id）"},
	{Key: KeyUserID, Const: "KeyUserID", Kind: KindID, Meaning: "用户标识（users.id）"},
	{Key: KeyRequestID, Const: "KeyRequestID", Kind: KindID, Meaning: "HTTP 请求追踪标识"},
	{Key: KeyDay, Const: "KeyDay", Kind: KindDay, Meaning: "业务日期（YYYY-MM-DD）"},
	{Key: KeyLastSuccessDay, Const: "KeyLastSuccessDay", Kind: KindDay, Meaning: "最近一次成功签到的业务日期；never 表示保留窗口内无成功"},
	{Key: KeyDurationMS, Const: "KeyDurationMS", Kind: KindDuration, Meaning: "耗时，整数毫秒"},
	{Key: KeyCount, Const: "KeyCount", Kind: KindCount, Meaning: "通用计数"},
	{Key: KeyRemaining, Const: "KeyRemaining", Kind: KindCount, Meaning: "尚未处理完的行数"},
	{Key: KeyBatch, Const: "KeyBatch", Kind: KindCount, Meaning: "单批处理条数"},
	{Key: KeyAttempt, Const: "KeyAttempt", Kind: KindCount, Meaning: "当前是第几次尝试（从 1 起）"},
	{Key: KeyFailedDays, Const: "KeyFailedDays", Kind: KindCount, Meaning: "保留窗口内的失败天数（含本次）"},
	{Key: KeyRowsDeleted, Const: "KeyRowsDeleted", Kind: KindCount, Meaning: "单轮已删除的库表行数"},
	{Key: KeyBlobsDeleted, Const: "KeyBlobsDeleted", Kind: KindCount, Meaning: "单轮已回收的照片 blob 数"},
	{Key: KeySessions, Const: "KeySessions", Kind: KindCount, Meaning: "已清理的过期会话数"},
	{Key: KeyResetTokens, Const: "KeyResetTokens", Kind: KindCount, Meaning: "已清理的过期重置令牌数"},
	{Key: KeyTasks, Const: "KeyTasks", Kind: KindCount, Meaning: "已清理的旧任务行数"},
	{Key: KeyNotifications, Const: "KeyNotifications", Kind: KindCount, Meaning: "已清理的过期站内信行数"},
	{Key: KeyBuilt, Const: "KeyBuilt", Kind: KindCount, Meaning: "本轮已构建的任务行数"},
	{Key: KeyBytes, Const: "KeyBytes", Kind: KindCount, Meaning: "HTTP 响应体字节数"},
	{Key: KeyTotal, Const: "KeyTotal", Kind: KindCount, Meaning: "某业务日的任务行总数（含未了结行）"},
	{Key: KeySuccess, Const: "KeySuccess", Kind: KindCount, Meaning: "某业务日 state=success 的任务行数"},
	{Key: KeyFailed, Const: "KeyFailed", Kind: KindCount, Meaning: "某业务日 state=failed 的任务行数"},
	{Key: KeySkipped, Const: "KeySkipped", Kind: KindCount, Meaning: "某业务日 state=skipped 的任务行数"},
	{Key: KeyPending, Const: "KeyPending", Kind: KindCount, Meaning: "某业务日尚未了结的任务行数（pending+running）"},
	{Key: KeyStatus, Const: "KeyStatus", Kind: KindNumber, Meaning: "HTTP 状态码"},
	{Key: KeyPort, Const: "KeyPort", Kind: KindText, Meaning: "SMTP 端口（配置标量）"},
	{Key: KeyPollInterval, Const: "KeyPollInterval", Kind: KindText, Meaning: "轮询间隔（时长串，如 2s）"},
	{Key: KeyHost, Const: "KeyHost", Kind: KindText, Meaning: "SMTP 主机名"},
	{Key: KeyTLSMode, Const: "KeyTLSMode", Kind: KindEnum, Meaning: "SMTP TLS 模式（封闭词表）"},
	{Key: KeyMethod, Const: "KeyMethod", Kind: KindEnum, Meaning: "HTTP 方法"},
	{Key: KeyPath, Const: "KeyPath", Kind: KindText, Meaning: "请求路径（不含查询串）"},
	{Key: KeyTrigger, Const: "KeyTrigger", Kind: KindEnum, Meaning: "构建触发源（daily/startup）"},
	{Key: KeyStage, Const: "KeyStage", Kind: KindEnum, Meaning: "签到阶段（discovery/submit/giveup）"},
	{Key: KeyOutcome, Const: "KeyOutcome", Kind: KindEnum, Meaning: "签到结论词（signing contract 词表）"},
	{Key: KeyOutcomeCounts, Const: "KeyOutcomeCounts", Kind: KindMap, Meaning: "失败 outcome → 任务行数的映射（键按 contract.Outcome 词表；未识别值原样保留，漂移可见；只含 state=failed 行）"},
	{Key: KeyReason, Const: "KeyReason", Kind: KindText, Meaning: "枚举或已掩码文本"},
	{Key: KeySuggestion, Const: "KeySuggestion", Kind: KindEnum, Meaning: "诊断建议词（core/diagnostic 词表）"},
	{Key: KeyCredentialKID, Const: "KeyCredentialKID", Kind: KindID, Meaning: "凭据指纹（credentials.kid）"},
	{Key: KeyHistoryAvailable, Const: "KeyHistoryAvailable", Kind: KindBool, Meaning: "签到历史查询是否可用"},
	{Key: KeyRetryable, Const: "KeyRetryable", Kind: KindBool, Meaning: "该失败是否可重试"},
	{Key: KeyError, Const: "KeyError", Kind: KindError, Meaning: "错误详情（写入前已掩码）"},
	{Key: KeyEvent, Const: "KeyEvent", Kind: KindEnum, Meaning: "事件词（如 password_reset.delivered）"},
	{Key: KeyEmail, Const: "KeyEmail", Kind: KindText, Meaning: "掩码后的邮箱标签"},
	{Key: KeyKind, Const: "KeyKind", Kind: KindEnum, Meaning: "失败粗分类（如 core/mail.Kind）"},
	{Key: KeyPanic, Const: "KeyPanic", Kind: KindText, Meaning: "panic 值的文本（不含请求数据）"},
}
