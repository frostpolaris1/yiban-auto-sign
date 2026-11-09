// Package notify 定义通知订阅的闭合词表与默认状态。
//
// # 功能
// 类别与渠道的枚举、合法判定、(类别, 渠道) 对与默认表。
// 本包**不**读写数据库、**不**投递通知、**不**判角色。
//
// # 归属
// core 层纯领域包。app/notify 提供用例，adapters/pg 提供覆盖表；
// 两者共用本包类型，故适配器无需导入 app。
//
// # 复用
// Category、Channel、Pair、Pref；Category*、Channel* 常量；Default、Categories、Channels。
//
// # 口径
// 默认表只有一份：review_pending=in_app+email+webhook；
// credential_invalid=in_app+webhook；sign_failed_daily=email；
// capacity_rejected=in_app。
// 表只存覆盖值；缺行按本表解释。
package notify

// Category 是通知主题的闭合词表。
type Category string

// 类别词表。顺序即响应顺序，与契约 enum 一致。
// sign_failed_daily 是**管理员订阅**的每日摘要渠道；
// 用户本人的签到失败邮件总开关是 users.mail_notify，
// 0v8 投递时两者分别判定，不互相替代。
const (
	CategoryCredentialInvalid Category = "credential_invalid"
	CategoryReviewPending     Category = "review_pending"
	CategorySignFailedDaily   Category = "sign_failed_daily"
	CategoryCapacityRejected  Category = "capacity_rejected"
)

// Channel 是投递渠道的闭合词表。三个渠道都已接线：
// in_app 落站内信行，email 走投递账本与重试，webhook 走
// serverchan/custom 两条出口的投递账本与重试。
type Channel string

// 渠道词表。in_app、email、webhook 是仅有的三个值；
// serverchan/custom 是 webhook 渠道内部的出口类型，不是独立渠道。
const (
	ChannelInApp   Channel = "in_app"
	ChannelEmail   Channel = "email"
	ChannelWebhook Channel = "webhook"
)

// categories 是稳定的类别顺序。响应按它展开。
var categories = []Category{
	CategoryCredentialInvalid,
	CategoryReviewPending,
	CategorySignFailedDaily,
	CategoryCapacityRejected,
}

// channels 是稳定的渠道顺序。
var channels = []Channel{ChannelInApp, ChannelEmail, ChannelWebhook}

// Categories 返回类别词表的副本，顺序稳定。
func Categories() []Category { return append([]Category(nil), categories...) }

// Channels 返回渠道词表的副本，顺序稳定。
func Channels() []Channel { return append([]Channel(nil), channels...) }

// Valid 报告类别是否在词表内。
func (c Category) Valid() bool {
	for _, known := range categories {
		if c == known {
			return true
		}
	}
	return false
}

// Valid 报告渠道是否在词表内。
func (ch Channel) Valid() bool {
	for _, known := range channels {
		if ch == known {
			return true
		}
	}
	return false
}

// Pair 是一个（类别, 渠道）组合，也是覆盖表的键。
type Pair struct {
	Category Category
	Channel  Channel
}

// Pref 是一对订阅的有效状态。
type Pref struct {
	Category Category
	Channel  Channel
	Enabled  bool
}

// defaults 是缺行时的默认状态。
// webhook 列默认只开 review_pending 与 credential_invalid：
// 手机推送留给需要人立刻处理的告警，日额度有限，日常报表类不挤占。
var defaults = map[Pair]bool{
	{Category: CategoryReviewPending, Channel: ChannelInApp}:       true,
	{Category: CategoryReviewPending, Channel: ChannelEmail}:       true,
	{Category: CategoryReviewPending, Channel: ChannelWebhook}:     true,
	{Category: CategoryCredentialInvalid, Channel: ChannelInApp}:   true,
	{Category: CategoryCredentialInvalid, Channel: ChannelEmail}:   false,
	{Category: CategoryCredentialInvalid, Channel: ChannelWebhook}: true,
	{Category: CategorySignFailedDaily, Channel: ChannelInApp}:     false,
	{Category: CategorySignFailedDaily, Channel: ChannelEmail}:     true,
	{Category: CategorySignFailedDaily, Channel: ChannelWebhook}:   false,
	{Category: CategoryCapacityRejected, Channel: ChannelInApp}:    true,
	{Category: CategoryCapacityRejected, Channel: ChannelEmail}:    false,
	{Category: CategoryCapacityRejected, Channel: ChannelWebhook}:  false,
}

// Default 返回一对的默认状态。未知对返回 false（零值）；
// 词表校验在用例层完成，未知对不会到达存储层。
func Default(pair Pair) bool { return defaults[pair] }
