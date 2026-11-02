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
// Record、New、Group、Counts。角色与状态词表取自 core/auth。
//
// # 口径
// 邮箱一律小写去空白后存储与比对。
// PasswordHash 是已算好的哈希串，本包不解释其格式。
// 账号计数只数未软删账号：AccountCount 全部、PendingCount 只 pending、
// ReviewCount 含 pending 与 rejected。
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
	// 名下未软删账号的计数。列表与抽屉共用。
	AccountCount      int
	PendingCount      int
	ReviewCount       int
	PasswordChangedAt *time.Time
	LastLoginAt       *time.Time
	CreatedAt         time.Time
	UpdatedAt         time.Time
	DeletedAt         *time.Time
	DeletedBy         string
}

// Group 是用户列表的展示分组，由名下未软删账号派生。
type Group string

const (
	GroupPending Group = "pending"
	GroupNormal  Group = "normal"
	GroupVacant  Group = "vacant"
)

// Valid 报告 g 是否为已知分组。
func (g Group) Valid() bool {
	switch g {
	case GroupPending, GroupNormal, GroupVacant:
		return true
	default:
		return false
	}
}

// Counts 是四个分组的总数。前三个只数未软删用户，Deleted 只数软删用户。
// 计数不随列表筛选或分页变化。
type Counts struct {
	Pending int
	Normal  int
	Vacant  int
	Deleted int
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
