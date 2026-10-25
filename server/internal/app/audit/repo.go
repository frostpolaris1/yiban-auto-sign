// Package audit 声明审计仓储端口。
//
// # 功能
// 声明追加与校验两个方法，并转发 core/audit 的类型。
// 本包**不**实现链编码、**不**取 HMAC 密钥、**不**写 SQL。
//
// # 归属
// app 层用例端口。链编码留在 core/audit，
// 这样 PostgreSQL 适配器无需导入本包（canonical.tsv 的 boundary.adapters）。
//
// # 复用
// Repo 接口；类型别名 Entry、Row、Report、Action。
//
// # 口径
// 追加**必须**是短事务的最后一条语句。
// 校验区间两端闭区间，0 表示该侧不限。
package audit

import (
	"context"

	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
)

// Entry 是一次追加的输入。
type Entry = coreaudit.Entry

// Row 是一行已存储的审计记录。
type Row = coreaudit.Row

// Report 汇总一次校验的区间。
type Report = coreaudit.Report

// Action 是受审计的操作名。
type Action = coreaudit.Action

// Repo 是审计仓储端口。
//
// Append **必须在事务内运行**。它取链 advisory lock、读当前链头、
// 插入下一行，故链是串行的，不会分叉。
// Append 必须是短事务的最后一条语句：
// 长事务持有链锁，会阻塞每一次受审计写入。
//
// Verify 校验链接连续性并复算每一行哈希。
// from 与 to 是闭区间行 id；0 表示该侧不限。
// **空区间是错误，永不算通过**。
type Repo interface {
	Append(ctx context.Context, in Entry) (Row, error)
	Verify(ctx context.Context, from, to int64) (Report, error)
}
