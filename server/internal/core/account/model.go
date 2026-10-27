// Package account 定义账号、凭据与账号状态的取值对象。
//
// # 功能
// 只定义账号状态词表与数据结构。本包**不**读写数据库，**不**校验状态迁移。
// 迁移规则归用例层；持久化归 adapters/pg。
//
// # 归属
// core 层纯领域包。外部依赖只有 time。app/account 与 adapters/pg 引用本包。
//
// # 复用
// Status 及 Valid、Active；Record、New、Credential。
//
// # 口径
// 账号状态与任务状态是两套词表（任务状态见 core/status）。
// 即使字面相同（active），两个值也不是同一对象，不得互相赋值。
package account

import "time"

// Status 是签到账号的生命周期状态。
type Status string

// 账号状态集合，v1 冻结。
// **credential_error 只由 P4 引擎写入**；其他写入方不得使用该值。
// 凭据更新（密码/设备码/机型）把 credential_error 恢复为 active。
const (
	StatusPending         Status = "pending"
	StatusActive          Status = "active"
	StatusRejected        Status = "rejected"
	StatusPausedAdmin     Status = "paused_admin"
	StatusPausedUser      Status = "paused_user"
	StatusCredentialError Status = "credential_error"
)

// Valid 报告 s 是否为已知状态。
func (s Status) Valid() bool {
	switch s {
	case StatusPending, StatusActive, StatusRejected, StatusPausedAdmin, StatusPausedUser, StatusCredentialError:
		return true
	default:
		return false
	}
}

// Active 报告账号是否可参与签到。
// **只有 active 账号可被引擎认领**；其他状态一律不进候选集。
func (s Status) Active() bool { return s == StatusActive }

// Record 是一行 accounts 记录。
type Record struct {
	ID           string
	SchoolID     string
	OwnerUserID  string
	Phone        string
	Status       Status
	RejectReason string
	ReviewedAt   *time.Time
	ReviewedBy   string
	CreatedAt    time.Time
	UpdatedAt    time.Time
	DeletedAt    *time.Time
	DeletedBy    string
}

// New 是创建账号的输入。ID 为空时由存储层补 UUIDv7。
type New struct {
	ID          string
	SchoolID    string
	OwnerUserID string
	Phone       string
}

// Credential 是一行加密凭据。
// **明文永不入数据库**；适配器原样存储这些字节，不再加解密。
type Credential struct {
	AccountID      string
	SchoolID       string
	Alg            string
	KID            string
	AADVersion     int16
	Nonce          []byte
	Ciphertext     []byte
	Version        int32
	CreatedAt      time.Time
	UpdatedAt      time.Time
	RekeyedAt      *time.Time
	LastVerifiedAt *time.Time
}
