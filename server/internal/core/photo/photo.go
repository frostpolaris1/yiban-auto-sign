// Package photo 定义图片库的领域取值与保留期判据。
//
// # 功能
// 定义一张图片的最小记录形状、允许的保留期闭集与到期判据。
// 本包**不**读写存储、**不**处理图片字节、**不**查库。
//
// # 归属
// core 层纯领域包。app/photos 声明端口，adapters/pg 与 adapters/storage 实现。
//
// # 复用
// Record、ValidRetention、DefaultRetentionDays、MaxBytes；Expired、Deleted。
//
// # 口径
// SHA256 是**预处理后**字节的摘要，与 blob 存储键一致。
// 保留期只接受 30/90/180/365 天；未设置时用 90 天。
package photo

import "time"

const (
	// DefaultRetentionDays 是用户未设置保留期时的天数。
	DefaultRetentionDays = 90
	// MaxBytes 是单张图片的原始上传上限（10 MiB）。
	MaxBytes = 10 << 20
)

// ValidRetention 报告 days 是否属于允许的保留期闭集。
func ValidRetention(days int) bool {
	switch days {
	case 30, 90, 180, 365:
		return true
	default:
		return false
	}
}

// Record 是一张已入库的图片。
// SHA256 是预处理后字节的摘要；DeletedAt 非空表示软删。
type Record struct {
	ID          string
	OwnerUserID string
	SHA256      [32]byte
	Mime        string
	Width       int
	Height      int
	Bytes       int64
	CreatedAt   time.Time
	DeletedAt   *time.Time
}

// Deleted 报告该行是否已软删。
func (r Record) Deleted() bool { return r.DeletedAt != nil }

// Expired 报告该行按 retentionDays 是否已到期。
// 软删行不算到期：它们由删除路径处理，避免重复清理。
func (r Record) Expired(now time.Time, retentionDays int) bool {
	if r.Deleted() || retentionDays <= 0 {
		return false
	}
	return now.Sub(r.CreatedAt) >= time.Duration(retentionDays)*24*time.Hour
}
