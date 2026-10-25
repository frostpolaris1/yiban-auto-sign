// Package school 声明院校与管理员绑定端口。
//
// # 功能
// 声明院校仓储与绑定仓储，并转发 core/school 的类型。
// 本包**不**实现 SQL、**不**校验绑定权限、**不**做停用级联。
//
// # 归属
// app 层用例端口。取值类型留在 core/school，
// 这样 PostgreSQL 适配器无需导入本包（canonical.tsv 的 boundary.adapters）。
//
// # 复用
// Repo、AdminRepo；类型别名 Status、Record、New、Update、Binding。
//
// # 口径
// 授予同一条绑定幂等；撤销不存在的绑定报 not_found。
package school

import (
	"context"

	coreschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/school"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// Status 是院校生命周期状态。
type Status = coreschool.Status

// 院校状态。值来自 core/school。
const (
	StatusActive   = coreschool.StatusActive
	StatusDisabled = coreschool.StatusDisabled
)

// Record 是一行 schools 记录。
type Record = coreschool.Record

// New 是创建院校的输入。
type New = coreschool.New

// Update 携带可选的院校列变更。nil 字段不改。
type Update = coreschool.Update

// Binding 是一行 admin_schools 记录。
type Binding = coreschool.AdminBinding

// Repo 是院校仓储端口。
//
// List 按作用域过滤并做游标分页。
// master 走全量语句；admin 走 id = ANY(...) 语句。
// **两条语句不合并**：空集合与全量必须可分辨。
type Repo interface {
	Create(ctx context.Context, in New) (Record, error)
	Get(ctx context.Context, id string) (Record, error)
	GetByCode(ctx context.Context, code string) (Record, error)
	Update(ctx context.Context, id string, in Update) (Record, error)
	List(ctx context.Context, scope corescope.Scope, cursor string, limit int) ([]Record, string, error)
}

// AdminRepo 是管理员绑定仓储端口。
//
// Grant 幂等：重复授予同一条绑定不算错误。
// **Revoke 删行；无行可删时报 not_found**。
// ListBySchool 带邮箱与角色，供管理页展示。
type AdminRepo interface {
	Grant(ctx context.Context, binding Binding) error
	Revoke(ctx context.Context, userID, schoolID string) error
	SchoolsForUser(ctx context.Context, userID string) ([]string, error)
	ListBySchool(ctx context.Context, schoolID, cursor string, limit int) ([]Binding, string, error)
	GetBinding(ctx context.Context, userID, schoolID string) (Binding, error)
}
