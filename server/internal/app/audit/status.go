// 本文件实现审计链巡检用例（2cu）。
//
// # 功能
// 把仓储的分窗链校验结果翻成巡检结论：ok / empty / broken 三态，
// 链头哈希转小写十六进制，并盖上本次巡检时刻。
// 挂上 AnchorReader 与 AnchorMetaReader 时，同时比对库外锚点与库内高水位：
// 删前缀、截尾、整表清空、链尾重签、锚点文件末行被删与文件行改写
// （含 count 改写：整行字节哈希失配）都会把结论翻成不健康。
//
// # 归属
// app 层用例；端口 ChainVerifier 由 adapters/pg 实现，
// AnchorReader 由 adapters/anchor 实现，AnchorMetaReader 由 adapters/pg 实现。
// 链算法、编码与校验规则留在 core/audit，本文件不改它们。
//
// # 口径
// 空链**不是通过**：零行什么都没证明（core 的 ErrEmptyChain 语义）。
// 故 ok=false、empty=true；前端据此显示「暂无审计行」而不是「异常」。
// 断链是巡检结论而不是错误：返回 ok=false + broken_at，调用方仍答 200。
// 锚点不一致同样是结论：ok=false + anchor_status=mismatch，调用方仍答 200。
// 锚点文件读不出/解析失败是错误结论：ok=false + anchor_status=error。
// 未配置锚点路径时 anchor_status=disabled，行为与引入锚点前相同。
// 从未写过锚点（文件与高水位都缺）时 anchor_status=missing，链本身照常判定。
// 文件有锚点但高水位缺失、或高水位读失败，是 error（无法检出截断）。
// 锚点文件末行被删/文件消失而高水位还在，是 mismatch + anchor_truncated。
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
// anchor 为 nil 时只校验链，不比对库外锚点；
// anchor 与 meta 都非 nil 时，core 同时比库内高水位（截断/count 改写/seq 倒退）。
type ChainVerifier interface {
	VerifyStatus(ctx context.Context, anchor *coreaudit.Anchor, meta *coreaudit.AnchorMeta) (coreaudit.Status, error)
}

// AnchorReader 读取库外锚点的最新一行。
// 文件不存在返回 (nil, nil)；文件存在但为空返回 coreaudit.ErrAnchorEmpty；
// 其它读取/解析错误原样上抛。
type AnchorReader interface {
	Latest(ctx context.Context) (*coreaudit.Anchor, error)
}

// AnchorMetaReader 读取库内锚点高水位。无记录返回 (nil, nil)。
type AnchorMetaReader interface {
	AnchorMeta(ctx context.Context) (*coreaudit.AnchorMeta, error)
}

// Inspector 是审计链巡检用例。
type Inspector struct {
	chain      ChainVerifier
	clock      clock.Clock
	anchors    AnchorReader
	anchorMeta AnchorMetaReader
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

// WithAnchorMeta 挂上库内锚点高水位读取器。
// 挂锚点文件时必须同时挂它：否则无法检出锚点文件末行被删与 count 改写。
func (i *Inspector) WithAnchorMeta(reader AnchorMetaReader) *Inspector {
	i.anchorMeta = reader
	return i
}

// Status 执行一次巡检。
// 数据源错误原样上抛；断链、空链与锚点不一致都作为结论返回，不当作错误。
func (i *Inspector) Status(ctx context.Context) (ChainStatus, error) {
	if i.anchors == nil {
		return i.statusFromChain(ctx, nil, nil, AnchorStatusDisabled, "")
	}
	anchor, anchorErr := i.anchors.Latest(ctx)
	if i.anchorMeta == nil {
		// 挂了锚点文件却没有高水位读取器：无法检出截断/count 改写，失败关闭。
		return i.statusFromChain(ctx, nil, nil, AnchorStatusError, "anchor_meta_reader_missing")
	}
	meta, metaErr := i.anchorMeta.AnchorMeta(ctx)
	switch {
	case metaErr != nil:
		return i.statusFromChain(ctx, nil, nil, AnchorStatusError, "anchor_meta_read_failed")
	case anchorErr == nil && anchor == nil:
		// 文件不存在：有高水位说明曾经写过，是截断；没有则从未锚定。
		if meta != nil {
			return i.statusFromChain(ctx, nil, nil, AnchorStatusMismatch, "anchor_truncated")
		}
		return i.statusFromChain(ctx, nil, nil, AnchorStatusMissing, "")
	case errors.Is(anchorErr, coreaudit.ErrAnchorEmpty):
		// 文件存在但为空：有高水位即截断；没有则从未锚定。
		if meta != nil {
			return i.statusFromChain(ctx, nil, nil, AnchorStatusMismatch, "anchor_truncated")
		}
		return i.statusFromChain(ctx, nil, nil, AnchorStatusMissing, "")
	case anchorErr != nil:
		// 文件存在但读不出/解析失败：不得当成"没有锚点"。
		return i.statusFromChain(ctx, nil, nil, AnchorStatusError, "anchor_read_failed")
	}
	if meta == nil {
		return i.statusFromChain(ctx, nil, nil, AnchorStatusError, "anchor_meta_missing")
	}
	// 文件与高水位都在：交给 core 做三字段（seq/head_hash/line_hash）与
	// 链内定点/行数比对；不一致的 reason 由 core 给出，这里只翻状态。
	return i.statusFromChain(ctx, anchor, meta, AnchorStatusOK, "")
}

// statusFromChain 跑链校验并装配结论。
// anchor 为 nil 时只校验链；anchorStatus 决定锚点字段与最终 OK。
func (i *Inspector) statusFromChain(ctx context.Context, anchor *coreaudit.Anchor,
	meta *coreaudit.AnchorMeta, anchorStatus AnchorStatus, anchorReason string) (ChainStatus, error) {
	raw, err := i.chain.VerifyStatus(ctx, anchor, meta)
	if err != nil {
		return ChainStatus{}, err
	}
	status := ChainStatus{
		CheckedRows:  raw.CheckedRows,
		CheckedAt:    i.clock.Now().UTC(),
		AnchorStatus: anchorStatus,
		AnchorReason: anchorReason,
	}
	if anchor != nil {
		status.AnchorSeq = anchor.Seq
		status.AnchorCount = anchor.Count
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
	switch anchorStatus {
	case AnchorStatusDisabled, AnchorStatusMissing:
		// 链结论照旧；锚点未参与判定。
	case AnchorStatusMismatch, AnchorStatusError:
		status.OK = false
	case AnchorStatusOK:
		if raw.BrokenAt != 0 {
			// 断链本身已是更直接的结论；锚点比对没有执行。
			status.AnchorStatus = AnchorStatusNotChecked
			status.AnchorReason = ""
			return status, nil
		}
		if !raw.Anchor.Checked {
			status.AnchorStatus = AnchorStatusError
			status.AnchorReason = "anchor_comparison_missing"
			status.OK = false
			return status, nil
		}
		if raw.Anchor.OK {
			status.AnchorOK = true
		} else {
			status.AnchorStatus = AnchorStatusMismatch
			status.AnchorReason = raw.Anchor.Reason
			status.OK = false
		}
	}
	return status, nil
}
