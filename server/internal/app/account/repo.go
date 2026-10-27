// Package account 声明账号与凭据的仓储端口。
//
// # 功能
// 声明账号仓储接口，并转发 core/account 的类型与常量。
// 本包**不**实现 SQL、**不**写用例编排、**不**判状态迁移合法性。
//
// # 归属
// app 层用例端口。取值类型留在 core/account，
// 这样 PostgreSQL 适配器无需导入本包（canonical.tsv 的 boundary.adapters）。
//
// # 复用
// Repo 接口；类型别名 Status、Record、New、Credential 及状态常量。
//
// # 口径
// 容量是活跃账号数（deleted_at IS NULL）。
// 越权行一律报 not_found，**不报 forbidden**。
package account

import (
	"context"
	"time"

	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// Status 是账号生命周期状态。
type Status = coreaccount.Status

// 账号状态。值来自 core/account。
const (
	StatusPending         = coreaccount.StatusPending
	StatusActive          = coreaccount.StatusActive
	StatusRejected        = coreaccount.StatusRejected
	StatusPausedAdmin     = coreaccount.StatusPausedAdmin
	StatusPausedUser      = coreaccount.StatusPausedUser
	StatusCredentialError = coreaccount.StatusCredentialError
)

// Record 是一行 accounts 记录。
type Record = coreaccount.Record

// New 是创建账号的输入。
type New = coreaccount.New

// Credential 是一行 credentials 记录。
type Credential = coreaccount.Credential

// Repo 是账号仓储端口。
//
// Create 与 Restore 执行活跃账号上限。两者**必须在事务内运行**：
// 在同一事务里先取账号容量 advisory lock，再统计活跃行。
// 条件语句命中零行时报 capacity_exceeded（errs.CodeCapacity）。
//
// SetStatus 是比较并交换：仅当当前状态等于 from 才改。
// 零行报 conflict（errs.CodeConflict）。**状态迁移规则归用例层，不归本端口**。
//
// Get、GetForUpdate、SetStatus、SoftDelete、Restore、Credential、UpdatePhone、Review 都套范围。
// 越权行报 not_found（errs.CodeNotFound），**绝不报 forbidden**。
//
// GetForUpdate 在事务内读账号并持有行锁到事务结束。
// 恢复判定必须在锁下读取：事务外的快照会把已提交的 credential_error 看丢。
//
// List 按作用域、可选院校、状态与搜索串过滤，并用游标分页。
// 过滤条件与作用域是**与**关系：未绑定院校的过滤结果为空，不是全量。
//
// Review 只推进 pending 行：通过写 active，拒绝写 rejected 并记理由。
// 非 pending 行报 conflict。
type Repo interface {
	Create(ctx context.Context, in New, limit int) (Record, error)
	Get(ctx context.Context, scope corescope.Scope, id string) (Record, error)
	GetForUpdate(ctx context.Context, scope corescope.Scope, id string) (Record, error)
	List(ctx context.Context, scope corescope.Scope, schoolID string, status Status, query, cursor string, limit int) ([]Record, string, error)
	UpdatePhone(ctx context.Context, scope corescope.Scope, id, phone string, at time.Time) (Record, error)
	SetStatus(ctx context.Context, scope corescope.Scope, id string, from, to Status, at time.Time, reason string) (Record, error)
	Review(ctx context.Context, scope corescope.Scope, id string, approve bool, reason, by string, at time.Time) (Record, error)
	SoftDelete(ctx context.Context, scope corescope.Scope, id, by string, at time.Time) error
	Restore(ctx context.Context, scope corescope.Scope, id string, limit int) (Record, error)
	Credential(ctx context.Context, scope corescope.Scope, accountID string) (Credential, error)
	InsertCredential(ctx context.Context, in Credential) error
	ReplaceCredential(ctx context.Context, in Credential, expectedVersion int32) error
}
