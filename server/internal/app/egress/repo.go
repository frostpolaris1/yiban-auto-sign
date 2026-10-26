// Package egress 实现账号到出口的粘性分配（P4-D）。
//
// # 功能
// 每个账号在首次使用时确定一个出口，此后永不迁移。
// 故障隔离的边界就是粘性绑定本身：出口故障只让该出口上的账号等待，
// 不把账号挪到别的出口（避免"故障即换 IP"的风控信号）。
// 分配在启用且健康的出口之间按账号 ID 确定性取模——不用 HRW、不做再平衡。
// 本包**不**发网络请求、**不**写 SQL。
//
// # 归属
// app 层用例。存储端口见本文件，实现见 adapters/pg。
//
// # 口径
// 已有绑定优先：即使该出口当前不健康也不迁移。
// 新账号只在 enabled 且未被标记失败的出口中选择；无可选出口报错。
// 并发首次分配靠库主键收敛：AssignOrGet 单语句，后到者读先到者的值。
package egress

import (
	"context"
	"errors"

	coreegress "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/egress"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

// Egress 是一个出口：取值对象在 core/egress（适配器不导入 app）。
type Egress = coreegress.Record

// Store 是出口与绑定的存储端口。
type Store interface {
	// Get 按 ID 取出口（**含停用**）：绑定出口被停用时仍要拿到 Identity。
	// 不存在返回 errs.ErrNotFound（egress_id 是 RESTRICT，正常不可达，属数据异常）。
	Get(ctx context.Context, egressID string) (Egress, error)
	// ListEnabled 返回启用出口，按 ID 升序（顺序参与确定性取模）。
	ListEnabled(ctx context.Context) ([]Egress, error)
	// GetAssignment 返回账号已绑定的出口 ID；无绑定返回 errs.ErrNotFound。
	GetAssignment(ctx context.Context, accountID string) (string, error)
	// AssignOrGet 原子地写入首选绑定并返回最终绑定：
	// 已有绑定时返回既有值（不覆盖）。
	AssignOrGet(ctx context.Context, accountID, preferredEgressID string) (string, error)
}

// isNotFound 报告是否"无绑定"。
func isNotFound(err error) bool {
	return err != nil && errors.Is(err, coreerrs.ErrNotFound)
}
