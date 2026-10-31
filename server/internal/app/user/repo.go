// Package user 声明用户仓储端口，并实现用户管理用例。
//
// # 功能
// 声明用户仓储接口与名下账号计数端口。建号见 create.go；
// 读面见 list.go 与 lookup.go；改角色见 update.go；
// 重置口令见 password_reset.go；软删见 delete.go；恢复见 restore.go。
// 本包**不**实现 SQL、**不**做 HTTP 编码、**不**签发会话。
//
// # 归属
// app 层端口与用例。Record 与 New 留在 core/user，
// 这样 PostgreSQL 适配器无需导入本包（canonical.tsv 的 boundary.adapters）。
// 别名让端口可读，并保证两侧命名同一类型。
//
// # 复用
// Repo、OwnerAccountCounter；类型别名 Record、New；
// create.go 的 Service、Deps、CreateInput。
//
// # 口径
// 用户上限数是活跃用户数（deleted_at IS NULL）。
// 邮箱比对一律小写去空白，实现取自 core/user。
package user

import (
	"context"
	"time"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// Record 是一行 users 记录。
type Record = coreuser.Record

// New 是创建用户的输入。
type New = coreuser.New

// Repo 是用户仓储端口。
//
// Create 执行活跃用户上限：**必须在事务内运行**，
// 先取用户容量 advisory lock 再计数。
// limit 是配置的有效用户上限。
// List 只返回未删除的行，并按 (created_at DESC, id DESC) 分页；
// includeDeleted 置真时软删行也入选。
// Restore 只恢复软删行，并在锁下重数活跃上限：**必须在事务内运行**。
// CountLiveMasters 在锁下统计未删 master：**必须在事务内运行**，
// 与 Create、Restore 同锁族，串行化并发的建号、降级与恢复。
// Lookup 按已归一邮箱前缀取未删行，按 email 升序取前 limit 条。
type Repo interface {
	Create(ctx context.Context, in New, limit int) (Record, error)
	GetByID(ctx context.Context, id string) (Record, error)
	GetByEmail(ctx context.Context, email string) (Record, error)
	List(ctx context.Context, role coreauth.Role, status coreauth.Status, query, cursor string, limit int, includeDeleted bool) ([]Record, string, error)
	UpdatePassword(ctx context.Context, id, passwordHash string, mustChange bool, at time.Time) error
	// RehashPassword 只替换哈希，**不改 password_changed_at**。
	// 登录时的透明升级走本方法：口令没有变，改密时刻不得前移。
	RehashPassword(ctx context.Context, id, passwordHash string) error
	UpdateRole(ctx context.Context, id string, role coreauth.Role, at time.Time) error
	SetStatus(ctx context.Context, id string, status coreauth.Status, at time.Time) error
	MarkLogin(ctx context.Context, id string, at time.Time) error
	SoftDelete(ctx context.Context, id, by string, at time.Time) error
	Restore(ctx context.Context, id string, limit int) (Record, error)
	CountLiveMasters(ctx context.Context, excludeID string) (int, error)
	Lookup(ctx context.Context, query string, limit int) ([]Record, error)
}

// OwnerAccountCounter 是名下活跃账号计数端口。
//
// 软删用户的判据需要数 accounts 表；账号用例的类型留在 account 包，
// 故这里只声明一个窄端口，由 assembly 注入实现。
// 用户包不 import 账号包。
type OwnerAccountCounter interface {
	CountLiveAccountsByOwner(ctx context.Context, userID string) (int, error)
}
