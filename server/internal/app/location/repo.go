package location

import (
	corelocation "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/location"
)

// 存储形状与端口实现在 core/location：
// 适配器只依赖 core，不依赖 app（canonical 的边界规则）。

// Pref 是写入存储的一行定位偏好。Point 为 nil 表示 auto（清空坐标）。
type Pref = corelocation.Pref

// Row 是一行已存储的定位偏好。
// auto 行的 Lng/Lat 无意义，恒为零值。
type Row = corelocation.Row

// Store 是定位偏好的存储端口（实现见 adapters/pg.LocationRepo）。
type Store = corelocation.Store
