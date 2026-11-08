// 本文件实现审计链巡检用例（2cu）。
//
// # 功能
// 把仓储的分窗链校验结果翻成巡检结论：ok / empty / broken 三态，
// 链头哈希转小写十六进制，并盖上本次巡检时刻。
//
// # 归属
// app 层用例；端口 ChainVerifier 由 adapters/pg 实现。
// 链算法、编码与校验规则留在 core/audit，本文件不改它们。
//
// # 口径
// 空链**不是通过**：零行什么都没证明（core 的 ErrEmptyChain 语义）。
// 故 ok=false、empty=true；前端据此显示「暂无审计行」而不是「异常」。
// 断链是巡检结论而不是错误：返回 ok=false + broken_at，调用方仍答 200。
// 响应只带行 id 与哈希，**不带审计正文、actor 或 detail**。
// CheckedRows 数到第一个不符行（含）；LastHash 是最后一个通过复算的行。
package audit

import (
	"context"
	"encoding/hex"
	"errors"
	"time"

	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
)

// ChainStatus 是一次巡检的结论。
//
// OK 只在一整条非空链全部复算通过时为 true。
// Empty 表示链内零行：没有证明任何事，不是通过，也不是断链。
// BrokenAt 是第一个不符行的行 id；与 OK=false、Empty=false 同时出现。
// LastHash 是最后一个通过复算的行的哈希（小写十六进制；没有通过行时为空串）。
type ChainStatus struct {
	OK          bool
	Empty       bool
	CheckedRows int64
	LastHash    string
	BrokenAt    *int64
	CheckedAt   time.Time
}

// ChainVerifier 读取并校验审计链。
// 实现必须自己持 HMAC 密钥，用例不接触密钥。
type ChainVerifier interface {
	VerifyStatus(ctx context.Context) (coreaudit.Status, error)
}

// Inspector 是审计链巡检用例。
type Inspector struct {
	chain ChainVerifier
	clock clock.Clock
}

// NewInspector 构造巡检器。缺端口或时钟即失败。
func NewInspector(chain ChainVerifier, clk clock.Clock) (*Inspector, error) {
	if chain == nil {
		return nil, errors.New("audit: nil chain verifier")
	}
	if clk == nil {
		return nil, errors.New("audit: nil clock")
	}
	return &Inspector{chain: chain, clock: clk}, nil
}

// Status 执行一次巡检。
// 数据源错误原样上抛；断链与空链都作为结论返回，不当作错误。
func (i *Inspector) Status(ctx context.Context) (ChainStatus, error) {
	raw, err := i.chain.VerifyStatus(ctx)
	if err != nil {
		return ChainStatus{}, err
	}
	status := ChainStatus{
		CheckedRows: raw.CheckedRows,
		CheckedAt:   i.clock.Now().UTC(),
	}
	if len(raw.LastHash) > 0 {
		status.LastHash = hex.EncodeToString(raw.LastHash)
	}
	switch {
	// 零行永远不是通过：即使端口漏标 Empty，也不得报健康。
	case raw.Empty || (raw.CheckedRows == 0 && raw.BrokenAt == 0):
		status.Empty = true
	case raw.BrokenAt != 0:
		broken := raw.BrokenAt
		status.BrokenAt = &broken
	default:
		status.OK = true
	}
	return status, nil
}
