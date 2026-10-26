package location

// 定位策略的存储形状。app/location 以别名暴露，adapters/pg 按本包实现；
// 这样适配器只依赖 core，不依赖 app（canonical 的边界规则）。

import (
	"context"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// Pref 是写入存储的一行定位偏好。Point 为 nil 表示 auto（清空坐标）。
type Pref struct {
	AccountID string
	Mode      string
	Point     *contract.Point
	Reason    string
	UpdatedBy string
	Now       time.Time
}

// Row 是一行已存储的定位偏好。
// auto 行的 Lng/Lat 无意义，恒为零值。
type Row struct {
	AccountID string
	Mode      string
	Lng       float64
	Lat       float64
	Reason    string
	UpdatedBy string
	UpdatedAt time.Time
}

// Store 读写 account_location 表。
type Store interface {
	// Get 读一行定位偏好；无行返回 errs.ErrNotFound。
	Get(ctx context.Context, accountID string) (Row, error)
	// Set 覆盖写一行。同一账号只保留最新策略。
	Set(ctx context.Context, in Pref) error
}
