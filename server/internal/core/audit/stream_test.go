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
