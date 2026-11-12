package audit_test

// 分窗链校验（巡检端点）的不变式。
//
// 口径：每窗至多 batchSize 行，内存 O(batchSize)；
// 窗与窗之间用上一窗末行哈希作锚点，下一窗首行的 prev_hash 必须等于它；
// 空链返回 Empty（没有证明任何事，不是通过）；
// 断链返回最靠前的定位行 id，CheckedRows 数到该行为止；
// 数据源错误原样上抛。

import (
	"bytes"
	"errors"
	"strings"
	"testing"
	"time"

	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
)

var streamKey = []byte("stream-test-audit-key-0123456789")

// streamChain 造一条 n 行的合法链，id 从 1 开始。
func streamChain(t *testing.T, n int) []coreaudit.Row {
	t.Helper()
	rows := make([]coreaudit.Row, n)
	prev := coreaudit.ZeroHash()
	for i := 0; i < n; i++ {
		row := coreaudit.Row{
			ID:         int64(i + 1),
			TS:         time.Date(2026, 11, 8, 8, i, 0, 0, time.UTC),
			ActorLabel: "master",
			ActorRole:  "master",
			Action:     "school.create",
			TargetType: "school",
			TargetID:   "school-a",
			Detail:     "{}",
			PrevHash:   prev,
		}
		row.Hash = coreaudit.Hash(streamKey, row)
		rows[i] = row
		prev = row.Hash
	}
	return rows
}

// sourceOf 把内存行按 id 升序切窗，模拟仓储的键集分页。
func sourceOf(rows []coreaudit.Row) coreaudit.RowSource {
	return func(afterID int64, limit int) ([]coreaudit.Row, error) {
		out := make([]coreaudit.Row, 0, limit)
		for _, row := range rows {
			if row.ID <= afterID {
				continue
			}
			out = append(out, row)
			if len(out) == limit {
				break
			}
		}
		return out, nil
	}
}

func TestVerifyStreamIntactChain(t *testing.T) {
	rows := streamChain(t, 5)
	status, err := coreaudit.VerifyStream(streamKey, 2, sourceOf(rows))
	if err != nil {
		t.Fatalf("VerifyStream: %v", err)
	}
	if status.Empty {
		t.Fatal("a five-row chain was reported empty")
	}
	if status.BrokenAt != 0 {
		t.Fatalf("broken_at = %d, want 0", status.BrokenAt)
	}
	if status.CheckedRows != 5 {
		t.Fatalf("checked rows = %d, want 5", status.CheckedRows)
	}
	if !bytes.Equal(status.LastHash, rows[4].Hash) {
		t.Fatalf("last hash = %x, want %x", status.LastHash, rows[4].Hash)
	}
}

func TestVerifyStreamEmptyChain(t *testing.T) {
	status, err := coreaudit.VerifyStream(streamKey, 2, sourceOf(nil))
	if err != nil {
		t.Fatalf("VerifyStream: %v", err)
	}
	if !status.Empty {
		t.Fatal("an empty chain was not reported empty")
	}
	if status.CheckedRows != 0 || status.BrokenAt != 0 || len(status.LastHash) != 0 {
		t.Fatalf("empty status = %+v, want zero values", status)
	}
}

// 同窗内篡改：定位到第一个坏行，CheckedRows 数到该行。
func TestVerifyStreamLocatesBrokenRow(t *testing.T) {
	rows := streamChain(t, 5)
	rows[3].Detail = `{"field":"reason"}` // 存储哈希仍是旧内容的，复算不符

	status, err := coreaudit.VerifyStream(streamKey, 5, sourceOf(rows))
	if err != nil {
		t.Fatalf("VerifyStream: %v", err)
	}
	if status.BrokenAt != 4 {
		t.Fatalf("broken_at = %d, want 4", status.BrokenAt)
	}
	if status.CheckedRows != 4 {
		t.Fatalf("checked rows = %d, want 4", status.CheckedRows)
	}
	if !bytes.Equal(status.LastHash, rows[2].Hash) {
		t.Fatalf("last hash = %x, want the last good row %x", status.LastHash, rows[2].Hash)
	}
}

// 跨窗断链：上一窗末行哈希与下一窗首行 prev_hash 不符。
// 下一窗首行自身哈希正确，故只有锚点比较能发现它。
func TestVerifyStreamLocatesBrokenLinkAcrossBatches(t *testing.T) {
	rows := streamChain(t, 5)
	rows[3].PrevHash = bytes.Repeat([]byte{0x33}, 32)
	rows[3].Hash = coreaudit.Hash(streamKey, rows[3])

	status, err := coreaudit.VerifyStream(streamKey, 2, sourceOf(rows))
	if err != nil {
		t.Fatalf("VerifyStream: %v", err)
	}
	if status.BrokenAt != 4 {
		t.Fatalf("broken_at = %d, want 4", status.BrokenAt)
	}
	if status.CheckedRows != 4 {
		t.Fatalf("checked rows = %d, want 4", status.CheckedRows)
	}
	if !bytes.Equal(status.LastHash, rows[2].Hash) {
		t.Fatalf("last hash = %x, want %x", status.LastHash, rows[2].Hash)
	}
}

// 首行（链锚点行）自身哈希不符：broken_at=1，此前没有通过的行。
func TestVerifyStreamBrokenFirstRow(t *testing.T) {
	rows := streamChain(t, 3)
	rows[0].ActorLabel = "tampered"
	status, err := coreaudit.VerifyStream(streamKey, 2, sourceOf(rows))
	if err != nil {
		t.Fatalf("VerifyStream: %v", err)
	}
	if status.BrokenAt != 1 || status.CheckedRows != 1 || len(status.LastHash) != 0 {
		t.Fatalf("status = %+v", status)
	}
}

func TestVerifyStreamPropagatesSourceError(t *testing.T) {
	sentinel := errors.New("source failed")
	_, err := coreaudit.VerifyStream(streamKey, 2, func(int64, int) ([]coreaudit.Row, error) {
		return nil, sentinel
	})
	if !errors.Is(err, sentinel) {
		t.Fatalf("err = %v, want the source error", err)
	}
}

// 数据源必须让游标前进；不前进会无限循环，必须拒绝。
func TestVerifyStreamRejectsNonAdvancingSource(t *testing.T) {
	rows := streamChain(t, 1)
	_, err := coreaudit.VerifyStream(streamKey, 1, func(int64, int) ([]coreaudit.Row, error) {
		return rows, nil
	})
	if err == nil {
		t.Fatal("a source that never advances was accepted")
	}
}

func TestVerifyStreamRejectsBadBatchSize(t *testing.T) {
	if _, err := coreaudit.VerifyStream(streamKey, 0, sourceOf(nil)); err == nil {
		t.Fatal("batch size 0 was accepted")
	}
	if _, err := coreaudit.VerifyStream(streamKey, 2, nil); err == nil {
		t.Fatal("a nil source was accepted")
	}
}

// anchorOf 用一条链的最后一行造锚点。
func anchorOf(rows []coreaudit.Row) *coreaudit.Anchor {
	last := rows[len(rows)-1]
	return &coreaudit.Anchor{Seq: last.ID, Count: int64(len(rows)), Hash: last.Hash}
}

// 锚点与链一致：比对执行且通过；锚点之后的新行不影响结论。
func TestVerifyStreamWithAnchorIntact(t *testing.T) {
	rows := streamChain(t, 5)
	status, err := coreaudit.VerifyStreamWithAnchor(streamKey, 2, sourceOf(rows), anchorOf(rows), nil)
	if err != nil {
		t.Fatalf("VerifyStreamWithAnchor: %v", err)
	}
	if !status.Anchor.Checked || !status.Anchor.OK {
		t.Fatalf("anchor = %+v, want checked and ok", status.Anchor)
	}
	if status.LastID != 5 {
		t.Fatalf("last id = %d, want 5", status.LastID)
	}
	// 锚点停在中间行：其后追加的行不在比对范围内。
	mid := anchorOf(rows[:3])
	status, err = coreaudit.VerifyStreamWithAnchor(streamKey, 2, sourceOf(rows), mid, nil)
	if err != nil {
		t.Fatalf("VerifyStreamWithAnchor(mid): %v", err)
	}
	if !status.Anchor.Checked || !status.Anchor.OK {
		t.Fatalf("mid anchor = %+v, want checked and ok", status.Anchor)
	}
}

// 删前缀：剩余链仍自洽（首行 prev_hash 不比对），只有锚点行数能发现。
func TestVerifyStreamWithAnchorDetectsPrefixDeletion(t *testing.T) {
	rows := streamChain(t, 5)
	status, err := coreaudit.VerifyStreamWithAnchor(streamKey, 2, sourceOf(rows[1:]), anchorOf(rows), nil)
	if err != nil {
		t.Fatalf("VerifyStreamWithAnchor: %v", err)
	}
	if status.BrokenAt != 0 {
		t.Fatalf("broken_at = %d; the remaining chain is internally consistent", status.BrokenAt)
	}
	if !status.Anchor.Checked || status.Anchor.OK {
		t.Fatalf("anchor = %+v, want checked and mismatch", status.Anchor)
	}
	if !strings.Contains(status.Anchor.Reason, "行数") {
		t.Fatalf("reason = %q, want a row-count mismatch", status.Anchor.Reason)
	}
}

// 截尾：链头 id 小于锚点 Seq，必红。
func TestVerifyStreamWithAnchorDetectsTailTruncation(t *testing.T) {
	rows := streamChain(t, 5)
	status, err := coreaudit.VerifyStreamWithAnchor(streamKey, 2, sourceOf(rows[:4]), anchorOf(rows), nil)
	if err != nil {
		t.Fatalf("VerifyStreamWithAnchor: %v", err)
	}
	if !status.Anchor.Checked || status.Anchor.OK {
		t.Fatalf("anchor = %+v, want checked and mismatch", status.Anchor)
	}
	if status.LastID != 4 {
		t.Fatalf("last id = %d, want 4", status.LastID)
	}
}

// 整表清空：空链带锚点不是"暂无行"，是锚点记录的链头消失。
func TestVerifyStreamWithAnchorDetectsEmptyChain(t *testing.T) {
	rows := streamChain(t, 3)
	status, err := coreaudit.VerifyStreamWithAnchor(streamKey, 2, sourceOf(nil), anchorOf(rows), nil)
	if err != nil {
		t.Fatalf("VerifyStreamWithAnchor: %v", err)
	}
	if !status.Empty {
		t.Fatal("empty chain was not reported empty")
	}
	if !status.Anchor.Checked || status.Anchor.OK {
		t.Fatalf("anchor = %+v, want checked and mismatch", status.Anchor)
	}
}

// 链被重签：行数与 id 都对得上，只有锚点哈希能发现。
func TestVerifyStreamWithAnchorDetectsResignedChain(t *testing.T) {
	rows := streamChain(t, 3)
	anchor := anchorOf(rows)
	anchor.Hash = bytes.Repeat([]byte{0x11}, 32)
	status, err := coreaudit.VerifyStreamWithAnchor(streamKey, 2, sourceOf(rows), anchor, nil)
	if err != nil {
		t.Fatalf("VerifyStreamWithAnchor: %v", err)
	}
	if status.BrokenAt != 0 {
		t.Fatalf("broken_at = %d; the chain is internally consistent", status.BrokenAt)
	}
	if !status.Anchor.Checked || status.Anchor.OK {
		t.Fatalf("anchor = %+v, want checked and mismatch", status.Anchor)
	}
	if !strings.Contains(status.Anchor.Reason, "哈希") {
		t.Fatalf("reason = %q, want a hash mismatch", status.Anchor.Reason)
	}
}

// 断链时不做锚点比对：断链本身已是更直接的结论。
func TestVerifyStreamWithAnchorSkipsComparisonOnBrokenChain(t *testing.T) {
	rows := streamChain(t, 5)
	rows[3].Detail = `{"field":"reason"}`
	status, err := coreaudit.VerifyStreamWithAnchor(streamKey, 2, sourceOf(rows), anchorOf(rows), nil)
	if err != nil {
		t.Fatalf("VerifyStreamWithAnchor: %v", err)
	}
	if status.BrokenAt != 4 {
		t.Fatalf("broken_at = %d, want 4", status.BrokenAt)
	}
	if status.Anchor.Checked {
		t.Fatalf("anchor = %+v, want not checked on a broken chain", status.Anchor)
	}
}

// 非法锚点值直接报错：不把接线缺陷伪装成链结论。
func TestVerifyStreamWithAnchorRejectsInvalidAnchor(t *testing.T) {
	rows := streamChain(t, 2)
	for _, anchor := range []*coreaudit.Anchor{
		{Seq: 0, Count: 0, Hash: rows[0].Hash},
		{Seq: 2, Count: 0, Hash: rows[1].Hash},
		{Seq: 2, Count: 3, Hash: rows[1].Hash},
		{Seq: 2, Count: 2, Hash: rows[1].Hash[:16]},
	} {
		if _, err := coreaudit.VerifyStreamWithAnchor(streamKey, 2, sourceOf(rows), anchor, nil); err == nil {
			t.Fatalf("invalid anchor %+v was accepted", anchor)
		}
	}
}

// ===== 库内高水位（meta）比对 =====

// metaLine 用指定整行字节哈希造高水位。
func metaLine(seq int64, headHash, lineHash []byte) *coreaudit.AnchorMeta {
	return &coreaudit.AnchorMeta{Seq: seq, HeadHash: headHash, LineHash: lineHash}
}

// 三字段全等：seq/head_hash/line_hash 一致，比对通过。
func TestVerifyStreamWithAnchorMetaMatch(t *testing.T) {
	rows := streamChain(t, 5)
	anchor := anchorOf(rows)
	anchor.LineHash = bytes.Repeat([]byte{0x21}, 32)
	meta := metaLine(anchor.Seq, anchor.Hash, anchor.LineHash)
	status, err := coreaudit.VerifyStreamWithAnchor(streamKey, 2, sourceOf(rows), anchor, meta)
	if err != nil {
		t.Fatalf("VerifyStreamWithAnchor: %v", err)
	}
	if !status.Anchor.Checked || !status.Anchor.OK {
		t.Fatalf("anchor = %+v, want checked and ok", status.Anchor)
	}
}

// count 改写：链内哈希不变，但整行字节哈希与 meta 不符，判 anchor_rewritten。
func TestVerifyStreamWithAnchorMetaDetectsLineRewrite(t *testing.T) {
	rows := streamChain(t, 5)
	anchor := anchorOf(rows)
	anchor.LineHash = bytes.Repeat([]byte{0x22}, 32)
	meta := metaLine(anchor.Seq, anchor.Hash, bytes.Repeat([]byte{0x23}, 32))
	status, err := coreaudit.VerifyStreamWithAnchor(streamKey, 2, sourceOf(rows), anchor, meta)
	if err != nil {
		t.Fatalf("VerifyStreamWithAnchor: %v", err)
	}
	if status.Anchor.OK || !status.Anchor.Checked {
		t.Fatalf("anchor = %+v, want checked and mismatch", status.Anchor)
	}
	if status.Anchor.Reason != "anchor_rewritten" {
		t.Fatalf("reason = %q, want anchor_rewritten", status.Anchor.Reason)
	}
}

// 文件最强 seq 低于 meta：锚点文件被截断，判 anchor_truncated。
func TestVerifyStreamWithAnchorMetaDetectsTruncatedFile(t *testing.T) {
	rows := streamChain(t, 5)
	anchor := anchorOf(rows[:3])
	anchor.LineHash = bytes.Repeat([]byte{0x24}, 32)
	meta := metaLine(5, rows[4].Hash, bytes.Repeat([]byte{0x25}, 32))
	status, err := coreaudit.VerifyStreamWithAnchor(streamKey, 2, sourceOf(rows[:3]), anchor, meta)
	if err != nil {
		t.Fatalf("VerifyStreamWithAnchor: %v", err)
	}
	if status.Anchor.OK {
		t.Fatalf("anchor = %+v, want mismatch", status.Anchor)
	}
	if status.Anchor.Reason != "anchor_truncated" {
		t.Fatalf("reason = %q, want anchor_truncated", status.Anchor.Reason)
	}
}

// 文件最强 seq 高于 meta：meta 写失败或有人追加未登记行，判 anchor_unrecorded。
func TestVerifyStreamWithAnchorMetaDetectsUnrecordedFile(t *testing.T) {
	rows := streamChain(t, 5)
	anchor := anchorOf(rows)
	anchor.LineHash = bytes.Repeat([]byte{0x26}, 32)
	meta := metaLine(3, rows[2].Hash, bytes.Repeat([]byte{0x27}, 32))
	status, err := coreaudit.VerifyStreamWithAnchor(streamKey, 2, sourceOf(rows), anchor, meta)
	if err != nil {
		t.Fatalf("VerifyStreamWithAnchor: %v", err)
	}
	if status.Anchor.OK {
		t.Fatalf("anchor = %+v, want mismatch", status.Anchor)
	}
	if status.Anchor.Reason != "anchor_unrecorded" {
		t.Fatalf("reason = %q, want anchor_unrecorded", status.Anchor.Reason)
	}
}
