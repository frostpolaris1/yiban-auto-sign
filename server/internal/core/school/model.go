// Package school 定义院校与管理员绑定的取值对象。
//
// # 功能
// 定义院校状态词表、院校记录与管理员绑定记录。
// 本包**不**读写数据库，**不**校验绑定权限，**不**取消绑定。
//
// # 归属
// core 层纯领域包。app/school 声明端口，adapters/pg 实现。
// 角色词表取自 core/auth。
//
// # 复用
// Status 及 Valid；Record、New、Update、AdminBinding。
//
// # 口径
// 停用院校保留数据，只停止新工作，**不删除**。
// Update 中 nil 指针表示不改该列，空串表示写入空值。
package school

import (
	"time"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

// Status 是院校的生命周期状态。
// 停用院校保留数据并停止新工作，**不删除**。
type Status string

// 院校状态集合，v1 冻结。
const (
	StatusActive   Status = "active"
	StatusDisabled Status = "disabled"
)

// Valid 报告 s 是否为已知状态。
func (s Status) Valid() bool { return s == StatusActive || s == StatusDisabled }

// Record 是一行 schools 记录。
type Record struct {
	ID        string
	Code      string
	Name      string
	Status    Status
	TimeZone  string
	CreatedAt time.Time
	UpdatedAt time.Time
}

// New 是创建院校的输入。
// TimeZone 可为空，此时套用列默认值。
type New struct {
	ID       string
	Code     string
	Name     string
	TimeZone string
}

// Update 携带可选的列变更。**nil 字段表示不改**。
type Update struct {
	Name     *string
	Status   *Status
	TimeZone *string
}

// AdminBinding 把一个用户绑定为一个院校的管理员。
type AdminBinding struct {
	UserID    string
	SchoolID  string
	GrantedBy string
	CreatedAt time.Time
	Email     string
	Role      coreauth.Role
}
