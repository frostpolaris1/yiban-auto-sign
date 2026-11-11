// 本文件实现审计链巡检用例（2cu）。
//
// # 功能
// 把仓储的分窗链校验结果翻成巡检结论：ok / empty / broken 三态，
// 链头哈希转小写十六进制，并盖上本次巡检时刻。
// 挂上 AnchorReader 时，同时比对库外锚点：删前缀、截尾、整表清空与
// 链尾重签都会把结论翻成不健康。
//
// # 归属
// app 层用例；端口 ChainVerifier 由 adapters/pg 实现，
// AnchorReader 由 adapters/anchor 实现。链算法、编码与校验规则留在 core/audit，
// 本文件不改它们。
//
// # 口径
// 空链**不是通过**：零行什么都没证明（core 的 ErrEmptyChain 语义）。
// 故 ok=false、empty=true；前端据此显示「暂无审计行」而不是「异常」。
// 断链是巡检结论而不是错误：返回 ok=false + broken_at，调用方仍答 200。
// 锚点不一致同样是结论：ok=false + anchor_status=mismatch，调用方仍答 200。
// 锚点文件读不出/为空是错误结论：ok=false + anchor_status=error（不冒充健康）。
// 未配置锚点路径时 anchor_status=disabled，行为与引入锚点前相同。
// 锚点文件不存在时 anchor_status=missing，链本身照常判定。
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

// AnchorStatus 是锚点状态词。它是响应字段的闭集，不是自由文本。
type AnchorStatus string

const (
	AnchorStatusDisabled   AnchorStatus = "disabled"
	AnchorStatusMissing    AnchorStatus = "missing"
	AnchorStatusOK         AnchorStatus = "ok"
	AnchorStatusMismatch   AnchorStatus = "mismatch"
	AnchorStatusError      AnchorStatus = "error"
	AnchorStatusNotChecked AnchorStatus = "not_checked"
)

// ChainStatus 是一次巡检的结论。
//
// OK 只在一整条非空链全部复算通过、且（若配置了锚点）锚点比对通过时为 true。
// Empty 表示链内零行：没有证明任何事，不是通过，也不是断链。
// BrokenAt 是第一个不符行的行 id；与 OK=false、Empty=false 同时出现。
// LastHash 是最后一个通过复算的行的哈希（小写十六进制；没有通过行时为空串）。
// AnchorStatus 是锚点状态词；AnchorOK 只在 AnchorStatus=ok 时为 true。
type ChainStatus struct {
	OK          bool
	Empty       bool
	CheckedRows int64
	LastHash    string
	BrokenAt    *int64
	CheckedAt   time.Time

	AnchorStatus AnchorStatus
	AnchorOK     bool
	AnchorReason string
	AnchorSeq    int64
	AnchorCount  int64
}

// ChainVerifier 读取并校验审计链。
// 实现必须自己持 HMAC 密钥，用例不接触密钥。
// anchor 为 nil 时只校验链，不比对库外锚点。
type ChainVerifier interface {
	VerifyStatus(ctx context.Context, anchor *coreaudit.Anchor) (coreaudit.Status, error)
}

// AnchorReader 读取库外锚点的最新一行。
// 文件不存在返回 (nil, nil)；文件存在但为空返回 coreaudit.ErrAnchorEmpty；
// 其它读取/解析错误原样上抛。
type AnchorReader interface {
	Latest(ctx context.Context) (*coreaudit.Anchor, error)
}

// Inspector 是审计链巡检用例。
type Inspector struct {
	chain   ChainVerifier
	clock   clock.Clock
	anchors AnchorReader
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

// WithAnchor 挂上库外锚点读取器。未挂时锚点比对不执行。
func (i *Inspector) WithAnchor(reader AnchorReader) *Inspector {
	i.anchors = reader
	return i
}

// Status 执行一次巡检。
// 数据源错误原样上抛；断链、空链与锚点不一致都作为结论返回，不当作错误。
func (i *Inspector) Status(ctx context.Context) (ChainStatus, error) {
	var anchor *coreaudit.Anchor
	var anchorErr error
	if i.anchors != nil {
		anchor, anchorErr = i.anchors.Latest(ctx)
		if errors.Is(anchorErr, coreaudit.ErrAnchorEmpty) {
			anchor = nil
		}
	}
	raw, err := i.chain.VerifyStatus(ctx, anchor)
	if err != nil {
		return ChainStatus{}, err
	}
	status := ChainStatus{
		CheckedRows:  raw.CheckedRows,
		CheckedAt:    i.clock.Now().UTC(),
		AnchorStatus: AnchorStatusDisabled,
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
	if i.anchors == nil {
		return status, nil
	}
	switch {
	case anchorErr != nil:
		// 锚点文件存在但读不出/为空：不得当成"没有锚点"。
		status.AnchorStatus = AnchorStatusError
		status.AnchorReason = "锚点文件读取失败: " + anchorErr.Error()
		status.OK = false
	case anchor == nil:
		// 未写锚点：沿用引入锚点前的行为，只把状态如实报出。
		status.AnchorStatus = AnchorStatusMissing
	case raw.BrokenAt != 0:
		// 断链本身已是更直接的结论；锚点比对没有执行。
		status.AnchorStatus = AnchorStatusNotChecked
	default:
		status.AnchorSeq = anchor.Seq
		status.AnchorCount = anchor.Count
		if !raw.Anchor.Checked {
			status.AnchorStatus = AnchorStatusError
			status.AnchorReason = "锚点比对未执行"
			status.OK = false
			return status, nil
		}
		if raw.Anchor.OK {
			status.AnchorStatus = AnchorStatusOK
			status.AnchorOK = true
			return status, nil
		}
		status.AnchorStatus = AnchorStatusMismatch
		status.AnchorReason = raw.Anchor.Reason
		status.OK = false
	}
	return status, nil
}
