// Package retry 是重试预算的唯一定义。
//
// # 功能
// 定义一次重试预算：最大尝试数、等待间隔，以及"会话是否已被污染"的分类标记。
// 标记只供哨兵分档（conformance 用它识别 default 档），本包不执行清会话。
// 本包**不**执行重试、**不**读配置、**不**算退避曲线。
//
// # 归属
// core 层纯领域包。用例层把 Policy 交给执行方。
//
// # 复用
// Policy、New。
//
// # 口径
// MaxAttempts 至少为 1；小于 1 的输入一律夹到 1。
// After 是两次尝试之间的等待时长。
package retry

import "time"

// Policy 是一份重试预算。
//
// SessionTainted 不是"宿主现在去清会话"的指令，而是**分类标记**：
// 它说明该结论意味着已建立的会话不可信（challenge、凭据错、词表外）。
// 只标记不执行的原因是归属：清会话属于提供方的会话面（宿主这侧没有
// 会话句柄），yiban 提供方拿到 999 时自清，宿主再清一遍只是第二套语义。
// 保留该标记是因为它同时是**词表哨兵**：MaxAttempts==1 && SessionTainted
// 只会来自 challenge/credential_invalid 两个已知词，或 BudgetFor 的 default 档
// ——conformance 用这个形状钉住"每个结论词都必须有显式分支"
// （signing/conformance/retry_vocab_test.go 显式豁免前两个词）。
type Policy struct {
	MaxAttempts int
	After       time.Duration
	// SessionTainted 表示该结论说明会话已不可信（分类标记，非动作）。
	SessionTainted bool
}

// New 构造预算，把尝试数夹到至少 1。
// **不夹会让 0 次尝试变成从不执行**。
func New(max int, after time.Duration) Policy {
	if max < 1 {
		max = 1
	}
	return Policy{MaxAttempts: max, After: after}
}
