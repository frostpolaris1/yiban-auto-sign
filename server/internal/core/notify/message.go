package notify

// 本文件定义站内信与投递账本的存储无关读模型。
//
// 归属：core/notify 纯领域包。app/notify 的用例与 adapters/pg 的仓储
// 共用这些类型，故适配器不必导入 app 层。
//
// 口径：Category、Channel 只复用本包既有词表，不在此新增取值。
// 投递状态是账本列的内部字符串（pending|sent|failed|skipped），
// 由投递用例解释，本包不导出状态词表。

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
