// Package retry 是重试预算的唯一定义。
//
// # 功能
// 定义一次重试预算：最大尝试数、等待间隔、失败后是否清会话。
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
type Policy struct {
	MaxAttempts  int
	After        time.Duration
	ClearSession bool
}

// New 构造预算，把尝试数夹到至少 1。
// **不夹会让 0 次尝试变成从不执行**。
func New(max int, after time.Duration) Policy {
	if max < 1 {
		max = 1
	}
	return Policy{MaxAttempts: max, After: after}
}
