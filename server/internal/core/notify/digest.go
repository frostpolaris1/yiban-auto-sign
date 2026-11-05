package notify

// 本文件定义每日签到失败摘要的存储无关模型。
//
// 归属：core/notify 纯领域包。app/notify 的摘要用例与 adapters/pg 的
// 仓储共用这些类型，故适配器不必导入 app 层。
//
// 口径：FailedTask 是 sign_tasks 的一行失败明细（任务维度），
// DigestSubscriber 是一位管理员收件人及其绑定校与订阅覆盖。
// 按账号聚合、掩码、文案与去重键全在 app/notify；本包不写 SQL、
// 不判默认订阅、不解析角色。

import "time"

// FailedTask 是一条签到失败明细，供每日摘要按账号聚合。
// Phone 是账号手机号原文，**落库前必须经 mask.MaskPhone**。
// Result 是任务结论词，空串由用例显示为 unknown。
type FailedTask struct {
	AccountID string
	SchoolID  string
	Phone     string
	Result    string
	Attempts  int
	UpdatedAt time.Time
}

// DigestSubscriber 是一位管理员摘要收件人。
// Master 为真时收全量；否则只收 Schools 内的院校。
// Schools 为空表示没有绑定校：该收件人什么都收不到。
// Overrides 只含显式覆盖行；缺行由用例按默认表解释。
type DigestSubscriber struct {
	UserID    string
	Email     string
	Master    bool
	Schools   []string
	Overrides map[Pair]bool
}
