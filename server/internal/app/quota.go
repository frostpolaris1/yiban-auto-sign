// Package app 内的配额读取端口（批 r4r）。
//
// # 功能
// QuotaLimits 提供生效的活跃用户/账号上限：0=不限，正数=上限。
// 消费方是 user/account/auth 用例；每请求解析一次，写回热生效。
//
// # 归属
// app 层端口。生产实现是 app/settings.Resolver（DB 覆盖链，缺行=1/1）；
// 测试与一次性装配用各自的固定替身。
//
// # 口径
// 0 是「不限」哨兵，不是「零额度」：计数检查整体跳过。
// 负数不是合法上限：实现方按域 [0,100000] 拒绝或回退，
// 消费方不猜负数语义。
package app

import "context"

// QuotaLimits 提供生效的活跃用户/账号上限。
// 两条线各自独立：用户键管全站活跃用户，账号键管全站活跃账号；
// 一人一号的 per-owner 规则不经过本端口。
type QuotaLimits interface {
	// UserLimit 返回全站活跃用户上限；0=不限。
	UserLimit(ctx context.Context) (int, error)
	// AccountLimit 返回全站活跃账号上限；0=不限。
	AccountLimit(ctx context.Context) (int, error)
}
