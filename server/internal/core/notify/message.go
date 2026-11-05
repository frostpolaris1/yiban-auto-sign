package notify

// 本文件定义站内信、投递账本与投递事件的存储无关模型。
//
// 归属：core/notify 纯领域包。app/notify 的用例与 adapters/pg 的仓储
// 共用这些类型，故适配器不必导入 app 层。
//
// 口径：Category、Channel 只复用本包既有词表，不在此新增取值。
// 投递状态是账本列的内部字符串（pending|sent|failed|skipped），
// 由投递用例解释，本包不导出状态词表。
// Event 与 UserFailure 是业务点交给投递用例的事件事实；文案、
// 收件人解析与掩码都留在 app/notify。

import "time"

// 投递账本的 status 取值。站内信渠道不建账本行，故此词表只服务邮件投递。
// 存储与用例共用这些常量；数据库不加 CHECK（同一不变式只留一道守卫）。
const (
	DeliveryStatusPending = "pending"
	DeliveryStatusSent    = "sent"
	DeliveryStatusFailed  = "failed"
	DeliveryStatusSkipped = "skipped"
)

// Notification 是站内信的一行。
// SchoolID 为空串表示该事件不属于某一院校。
// ReadAt 为 nil 表示未读。
type Notification struct {
	ID        string
	Category  Category
	Title     string
	Body      string
	SchoolID  string
	CreatedAt time.Time
	ReadAt    *time.Time
}

// NotificationInsert 是写入一条站内信的参数。
// DedupeKey 是 (user, dedupe_key) 幂等键的业务部分。
type NotificationInsert struct {
	UserID    string
	Category  Category
	DedupeKey string
	Title     string
	Body      string
	SchoolID  string
	CreatedAt time.Time
}

// NotificationQuery 是站内信列表的读参数。
// Cursor 为空串表示首页；Limit 由用例定型为 1..100。
type NotificationQuery struct {
	UserID string
	Cursor string
	Limit  int
}

// Delivery 是投递账本的一行。
// Status 与 LastErrorKind 是账本内部字符串，对客户端不可见。
// LastErrorKind 只存 core/mail.Kind 词表；SkipReason 只存跳过原因。
// Attempts 是**发送尝试数**：MarkSent 与 MarkFailed 都递增，
// skipped 不是尝试、不递增。
type Delivery struct {
	ID             string
	NotificationID string
	UserID         string
	Channel        Channel
	Status         string
	Attempts       int
	LastErrorKind  string
	SkipReason     string
	NextRetryAt    *time.Time
	CreatedAt      time.Time
	UpdatedAt      time.Time
}

// DeliveryInsert 是写入一条待投递账本行的参数。
// NextRetryAt 为 nil 时由存储层取当前时刻。
type DeliveryInsert struct {
	NotificationID string
	UserID         string
	Channel        Channel
	NextRetryAt    *time.Time
}

// NotificationWithDelivery 是一次原子写入的结果：
// 通知行与（可选）待投递账本行。Delivery 的 ID 为空表示未入账本。
type NotificationWithDelivery struct {
	Notification Notification
	Delivery     Delivery
}

// DueDelivery 是一条待投递的邮件账本行，连同投递所需的正文与收件地址。
// 读者只应看到本类型；账本行的状态机仍由 NotificationDeliveryRepo 维护。
//
// Title 与 Body 是站内信落库后的文本（已掩码）；Email 是收件地址，
// 只用于 SMTP 信封，**不得**进日志。
type DueDelivery struct {
	Delivery
	Title string
	Body  string
	Email string
}

// Event 是一次管理员通知事件。
//
// 业务点只填事件事实，收件人解析、文案、掩码与投递全在 app/notify：
// 适配器因此不必导入 app 层。AccountID 为空表示事件不针对单个账号
// （capacity_rejected）；Capacity 只对容量事件有效。
type Event struct {
	Category  Category
	SchoolID  string
	AccountID string
	DedupeKey string
	Day       string
	Capacity  CapacityFigures
}

// CapacityFigures 是容量缺口事件的数字面，与计划的 Preview 同源。
// SuggestedWorkers 为 0 表示不给建议。
type CapacityFigures struct {
	Demand           int
	Capacity         int
	Shortfall        int
	Binding          string
	SuggestedWorkers int
}

// UserFailure 是用户本人的签到失败事件。
//
// 只描述事实：收件人是账号属主，开关判定、去重键与文案由 app/notify 负责。
// Reason 是结论原因的原文，**落库前必须掩码**。
type UserFailure struct {
	SchoolID  string
	AccountID string
	Day       string
	Outcome   string
	Reason    string
}

// Subscriber 是一位管理员收件人及其订阅覆盖。
// Overrides 只含显式覆盖行；缺行由用例按默认表解释。
type Subscriber struct {
	UserID    string
	Email     string
	Overrides map[Pair]bool
}

// Owner 是账号属主的失败邮件收件信息。
// MailNotify 是 users.mail_notify：用户本人失败邮件只看它，
// 不看 notification_prefs（两条订阅面分别判定，永不 OR）。
type Owner struct {
	UserID     string
	MailNotify bool
}
