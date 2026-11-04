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
// 口令或设备变更把 credential_error 恢复为 active；
// 改绑手机号例外：手机号即登录凭据，改绑一律回 pending 重审。
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

// SelfRestoreGrace 是本人撤销注销的宽限窗口：7 天。
// 取值与旧线 parity（yiban/adapters/persistence/users.py 的
// SOFT_DELETE_RETENTION_DAYS = 7）；窗口从 deleted_at 起算，
// 恰好 7 天仍可恢复，超过即拒绝。物理清除归保留期任务，不在本批。
const SelfRestoreGrace = 7 * 24 * time.Hour

// Record 是一行 accounts 记录。
//
// OwnerEmail 是属主邮箱的明文，只在进程内用于生成掩码响应；
// 入口层**不得**原样回显，掩码后才可以出服务端。
type Record struct {
	ID          string
	SchoolID    string
	OwnerUserID string
	Phone       string
	Name        string
	// DisplayName 是列表展示用的有效备注名：name 为空时派生「账号+手机尾 4 位」。
	// 搜索与展示共用这一个值，避免「看得到却搜不到」。
	DisplayName string
	// PhoneModel 是凭据明文里的机型，只用于展示。
	// 空串表示未设置或凭据不可读；**不是设备身份**，不进任何判据。
	PhoneModel   string
	Status       Status
	RejectReason string
	ReviewedAt   *time.Time
	ReviewedBy   string
	CreatedAt    time.Time
	UpdatedAt    time.Time
	DeletedAt    *time.Time
	DeletedBy    string
	OwnerEmail   string
}

// New 是创建账号的输入。ID 为空时由存储层补 UUIDv7。
// Name 是可选备注名，空串表示未命名。
type New struct {
	ID          string
	SchoolID    string
	OwnerUserID string
	Phone       string
	Name        string
}

// StatusCounts 是未软删账号的状态计数汇总，口径与账号列表同源：
// 只数 deleted_at IS NULL 的行。只覆盖审核面关心的两个状态。
type StatusCounts struct {
	Pending  int
	Rejected int
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
