// Package user 声明用户仓储端口。
//
// # 功能
// 声明用户仓储接口，并转发 core/user 的类型与 core/auth 的词表。
// 本包**不**实现 SQL、**不**校验密码、**不**签发会话。
//
// # 归属
// app 层用例端口。Record 与 New 留在 core/user，
// 这样 PostgreSQL 适配器无需导入本包（canonical.tsv 的 boundary.adapters）。
// 别名让端口可读，并保证两侧命名同一类型。
//
// # 复用
// Repo；类型别名 Record、New。
//
// # 口径
// 用户上限数是活跃用户数（deleted_at IS NULL）。
// 邮箱比对一律小写去空白。
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
type Repo interface {
	Create(ctx context.Context, in New, limit int) (Record, error)
	GetByID(ctx context.Context, id string) (Record, error)
	GetByEmail(ctx context.Context, email string) (Record, error)
	UpdatePassword(ctx context.Context, id, passwordHash string, mustChange bool, at time.Time) error
	SetStatus(ctx context.Context, id string, status coreauth.Status, at time.Time) error
	MarkLogin(ctx context.Context, id string, at time.Time) error
	SoftDelete(ctx context.Context, id, by string, at time.Time) error
}
