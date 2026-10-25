// Package user 定义用例层与 PostgreSQL 适配器共用的用户取值对象。
//
// # 功能
// 只定义用户行与创建输入。本包**不**声明仓储端口，
// 也**不**校验邮箱或密码。
//
// # 归属
// core 层纯领域包。仓储端口在 internal/app/user，
// 这样适配器不必导入 app（canonical.tsv 的 boundary.adapters）。
//
// # 复用
// Record、New。角色与状态词表取自 core/auth。
//
// # 口径
// 邮箱一律小写去空白后存储与比对。
// PasswordHash 是已算好的哈希串，本包不解释其格式。
package user

import (
	"time"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

// Record 是一行 users 记录。
type Record struct {
	ID                 string
	Email              string
	PasswordHash       string
	Role               coreauth.Role
	Status             coreauth.Status
	MustChangePassword bool
	PasswordChangedAt  *time.Time
	LastLoginAt        *time.Time
	CreatedAt          time.Time
	UpdatedAt          time.Time
	DeletedAt          *time.Time
	DeletedBy          string
}

// New 是创建用户的输入。
// ID 可选；为空时由存储层补 UUIDv7。
type New struct {
	ID                 string
	Email              string
	PasswordHash       string
	Role               coreauth.Role
	MustChangePassword bool
}
