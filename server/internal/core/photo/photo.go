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
// Record、Blob、ValidRetention、DefaultRetentionDays、MaxBytes；Expired、Deleted。
//
// # 口径
// SHA256 是**预处理后**字节的摘要，与 blob 存储键一致。
// 保留期闭集见 RetentionDays；未设置时用 DefaultRetentionDays。
// 到期判据从 COALESCE(deleted_at, created_at) 起算。
package photo

import "time"

// RetentionDays 是允许的保留期闭集（天），严格升序。
// 它是唯一权威：ValidRetention 与迁移 CHECK 约束都由本表派生语义，
// SQL 无法引用 Go 常量，故由 retention_single_source_test 钉住两侧相等。
var RetentionDays = []int{30, 90, 180, 365}

const (
	// DefaultRetentionDays 是用户未设置保留期时的天数。
	// 必须属于 RetentionDays；由测试钉住。
	DefaultRetentionDays = 90
	// MaxBytes 是单张图片的原始上传上限（10 MiB）。
	MaxBytes = 10 << 20
)

// ValidRetention 报告 days 是否属于允许的保留期闭集。
func ValidRetention(days int) bool {
	for _, allowed := range RetentionDays {
		if days == allowed {
			return true
		}
	}
	return false
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

// Blob 是一份已落盘图片字节的摘要与修改时间。
// 回收判据需要修改时间：刚落盘但行事务尚未提交的文件必须保留。
type Blob struct {
	Digest  [32]byte
	ModTime time.Time
}

// Deleted 报告该行是否已软删。
func (r Record) Deleted() bool { return r.DeletedAt != nil }

// Expired 报告该行按 retentionDays 是否已到期。
// 判据是 COALESCE(deleted_at, created_at)：软删行从删除时刻起算，
// 未到期不物理删。重传恢复同一行的语义依赖这段保留期。
func (r Record) Expired(now time.Time, retentionDays int) bool {
	if retentionDays <= 0 {
		return false
	}
	from := r.CreatedAt
	if r.DeletedAt != nil {
		from = *r.DeletedAt
	}
	return now.Sub(from) >= time.Duration(retentionDays)*24*time.Hour
}
