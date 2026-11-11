package pg_test

// anchor_meta 棘轮的真库 e2e。
//
// # 功能
// RecordAnchorMeta 的 seq 只进不退：meta 3→1 必须被拒且库内仍是 3；
// 同 seq 幂等重放放行；更高 seq 正常推进；非法值（seq 非正、哈希长度错）
// 一律 validation 拒绝。读回三字段与写入值一致。
//
// # 口径
// 棘轮是第二道防线：CLI 先行判截断/改写后才写 meta；走到这里还倒退
// 属缺陷，返回 validation 错误而不是静默吞掉。缺 DSN 时显性 SKIP。

import (
	"bytes"
	"testing"

	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
)

func anchorMetaOf(seq int64, head, line byte) coreaudit.AnchorMeta {
	return coreaudit.AnchorMeta{
		Seq:      seq,
		HeadHash: bytes.Repeat([]byte{head}, 32),
		LineHash: bytes.Repeat([]byte{line}, 32),
	}
}

func TestAnchorMetaRatchetE2E(t *testing.T) {
	e := newEnv(t)

	// 首次写入 seq=3：三字段读回一致。
	first := anchorMetaOf(3, 0x31, 0x41)
	if err := e.audits.RecordAnchorMeta(e.ctx, first); err != nil {
		t.Fatalf("RecordAnchorMeta(3): %v", err)
	}
	meta, err := e.audits.AnchorMeta(e.ctx)
	if err != nil || meta == nil {
		t.Fatalf("AnchorMeta = (%+v, %v), want a row", meta, err)
	}
	if meta.Seq != 3 || !bytes.Equal(meta.HeadHash, first.HeadHash) || !bytes.Equal(meta.LineHash, first.LineHash) {
		t.Fatalf("meta = %+v, want the written values", meta)
	}

	// 棘轮：meta 3→1 必须被拒，库内仍停在 3。
	rejected := anchorMetaOf(1, 0x32, 0x42)
	ratchetErr := e.audits.RecordAnchorMeta(e.ctx, rejected)
	if ratchetErr == nil {
		t.Fatal("ratchet accepted a lower seq")
	}
	requireValidation(t, ratchetErr)
	meta, err = e.audits.AnchorMeta(e.ctx)
	if err != nil || meta == nil {
		t.Fatalf("AnchorMeta after rejection = (%+v, %v), want the old row", meta, err)
	}
	if meta.Seq != 3 || !bytes.Equal(meta.LineHash, first.LineHash) {
		t.Fatalf("meta after rejection = %+v, want untouched seq=3", meta)
	}

	// 同 seq 幂等重放放行（内容允许刷新）。
	same := anchorMetaOf(3, 0x31, 0x41)
	if err := e.audits.RecordAnchorMeta(e.ctx, same); err != nil {
		t.Fatalf("RecordAnchorMeta(3 again): %v", err)
	}
	meta, err = e.audits.AnchorMeta(e.ctx)
	if err != nil || meta == nil || meta.Seq != 3 {
		t.Fatalf("meta after same-seq write = (%+v, %v), want seq=3", meta, err)
	}

	// 更高 seq 正常推进。
	forward := anchorMetaOf(4, 0x33, 0x43)
	if err := e.audits.RecordAnchorMeta(e.ctx, forward); err != nil {
		t.Fatalf("RecordAnchorMeta(4): %v", err)
	}
	meta, err = e.audits.AnchorMeta(e.ctx)
	if err != nil || meta == nil || meta.Seq != 4 {
		t.Fatalf("meta after forward write = (%+v, %v), want seq=4", meta, err)
	}

	// 非法值：seq 非正与哈希长度错都是 validation 拒绝。
	for name, bad := range map[string]coreaudit.AnchorMeta{
		"zero seq":     {Seq: 0, HeadHash: forward.HeadHash, LineHash: forward.LineHash},
		"short head":   {Seq: 5, HeadHash: forward.HeadHash[:16], LineHash: forward.LineHash},
		"short line":   {Seq: 5, HeadHash: forward.HeadHash, LineHash: forward.LineHash[:16]},
		"missing line": {Seq: 5, HeadHash: forward.HeadHash},
	} {
		t.Run(name, func(t *testing.T) {
			badErr := e.audits.RecordAnchorMeta(e.ctx, bad)
			if badErr == nil {
				t.Fatalf("invalid meta %+v was accepted", bad)
			}
			requireValidation(t, badErr)
		})
	}
}
