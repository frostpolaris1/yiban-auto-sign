// Package app 存放跨领域共用的用例端口。
//
// # 功能
// 定义事务边界。本包**不**实现事务、**不**写用例逻辑。
//
// # 归属
// app 层端口。适配器 adapters/pg 的 DB 实现 TxManager。
//
// # 复用
// TxManager。
//
// # 口径
// 事务经 context 传递。嵌套调用复用外层事务，不另开第二个。
package app

import "context"

// TxManager 在一个数据库事务内运行 fn。
//
// 事务经 context 传递。嵌套调用复用外层事务，**不另开第二个**。
// 实现使用 READ COMMITTED：容量计数必须在取到 advisory lock 后
// 读新快照（schema §6.5）。
type TxManager interface {
	InTx(ctx context.Context, fn func(ctx context.Context) error) error
}
