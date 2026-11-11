package anchor_test

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/anchor"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
)

func testAnchor(seq, count int64) coreaudit.Anchor {
	hash := sha256.Sum256([]byte{byte(seq), byte(count)})
	return coreaudit.Anchor{Seq: seq, Count: count, Hash: hash[:]}
}

// testAnchorTime 是测试用的固定锚点时刻（canonical 门禁止测试里直接 time.Now）。
var testAnchorTime = time.Date(2026, 11, 11, 1, 0, 0, 0, time.UTC)

func TestLatestMissingFile(t *testing.T) {
	file := anchor.NewFile(filepath.Join(t.TempDir(), "audit-anchor.log"))
	got, err := file.Latest(context.Background())
	if err != nil || got != nil {
		t.Fatalf("Latest = (%+v, %v), want (nil, nil)", got, err)
	}
}

func TestAppendAndLatest(t *testing.T) {
	path := filepath.Join(t.TempDir(), "audit-anchor.log")
	file := anchor.NewFile(path)
	first := testAnchor(3, 3)
	if err := file.Append(context.Background(), first, time.Date(2026, 11, 11, 1, 2, 3, 0, time.UTC)); err != nil {
		t.Fatalf("Append: %v", err)
	}
	got, err := file.Latest(context.Background())
	if err != nil {
		t.Fatalf("Latest: %v", err)
	}
	if got.Seq != first.Seq || got.Count != first.Count || string(got.Hash) != string(first.Hash) {
		t.Fatalf("Latest = %+v, want %+v", got, first)
	}
	// 第二个锚点 seq 更大：Latest 取最大 seq。
	second := testAnchor(5, 4)
	if err := file.Append(context.Background(), second, time.Date(2026, 11, 12, 1, 2, 3, 0, time.UTC)); err != nil {
		t.Fatalf("Append second: %v", err)
	}
	got, err = file.Latest(context.Background())
	if err != nil {
		t.Fatalf("Latest second: %v", err)
	}
	if got.Seq != second.Seq || got.Count != second.Count {
		t.Fatalf("Latest = %+v, want seq=%d count=%d", got, second.Seq, second.Count)
	}
	// 追加一条更小的 seq（回滚后的旧链头）：Latest 仍取最大，Last 取文件末行。
	third := testAnchor(2, 2)
	if err := file.Append(context.Background(), third, time.Date(2026, 11, 13, 1, 2, 3, 0, time.UTC)); err != nil {
		t.Fatalf("Append third: %v", err)
	}
	got, err = file.Latest(context.Background())
	if err != nil {
		t.Fatalf("Latest third: %v", err)
	}
	if got.Seq != second.Seq {
		t.Fatalf("Latest after smaller append = %d, want max %d", got.Seq, second.Seq)
	}
	last, err := file.Last(context.Background())
	if err != nil {
		t.Fatalf("Last: %v", err)
	}
	if last.Seq != third.Seq {
		t.Fatalf("Last = %d, want the final line %d", last.Seq, third.Seq)
	}
	if runtime.GOOS != "windows" {
		info, err := os.Stat(path)
		if err != nil {
			t.Fatalf("stat: %v", err)
		}
		if perm := info.Mode().Perm(); perm != 0o600 {
			t.Fatalf("mode = %o, want 600", perm)
		}
	}
}

// 空文件不是"从未写锚点"：可能是截断，必须返回 ErrEmptyFile。
func TestLatestEmptyFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "audit-anchor.log")
	if err := os.WriteFile(path, nil, 0o600); err != nil {
		t.Fatalf("write empty: %v", err)
	}
	if _, err := anchor.NewFile(path).Latest(context.Background()); !errors.Is(err, anchor.ErrEmptyFile) {
		t.Fatalf("err = %v, want ErrEmptyFile", err)
	}
}

// 任一行不可解析即报错；损坏文件拒绝追加。
func TestInvalidLineFailsClosed(t *testing.T) {
	path := filepath.Join(t.TempDir(), "audit-anchor.log")
	if err := os.WriteFile(path, []byte("v1 not-a-time 1 1 "+strings.Repeat("a", 64)+"\n"), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	file := anchor.NewFile(path)
	if _, err := file.Latest(context.Background()); err == nil {
		t.Fatal("invalid line was accepted")
	}
	if err := file.Append(context.Background(), testAnchor(2, 2), testAnchorTime); err == nil {
		t.Fatal("Append accepted an invalid anchor file")
	}
}

func TestAppendRejectsInvalidAnchor(t *testing.T) {
	file := anchor.NewFile(filepath.Join(t.TempDir(), "audit-anchor.log"))
	hash := testAnchor(1, 1).Hash
	for _, a := range []coreaudit.Anchor{
		{Seq: 0, Count: 0, Hash: hash},
		{Seq: 2, Count: 0, Hash: hash},
		{Seq: 2, Count: 3, Hash: hash},
		{Seq: 2, Count: 2, Hash: hash[:16]},
	} {
		if err := file.Append(context.Background(), a, testAnchorTime); err == nil {
			t.Fatalf("invalid anchor %+v was accepted", a)
		}
	}
}

func TestAppendRequiresPath(t *testing.T) {
	if err := anchor.NewFile("").Append(context.Background(), testAnchor(1, 1), testAnchorTime); err == nil {
		t.Fatal("empty path was accepted")
	}
}

// 末行缺换行时先补换行，避免新行拼在旧行尾部。
func TestAppendRepairsMissingFinalNewline(t *testing.T) {
	path := filepath.Join(t.TempDir(), "audit-anchor.log")
	first := testAnchor(1, 1)
	line := fmt.Sprintf("v1 %s %d %d %s",
		testAnchorTime.Format(time.RFC3339Nano), first.Seq, first.Count,
		hex.EncodeToString(first.Hash))
	if err := os.WriteFile(path, []byte(line), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	file := anchor.NewFile(path)
	if err := file.Append(context.Background(), testAnchor(2, 2), testAnchorTime); err != nil {
		t.Fatalf("Append: %v", err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	if !strings.Contains(string(data), line+"\n") {
		t.Fatalf("missing newline was not repaired: %q", data)
	}
	latest, err := file.Latest(context.Background())
	if err != nil {
		t.Fatalf("Latest: %v", err)
	}
	if latest.Seq != 2 {
		t.Fatalf("latest seq = %d, want 2", latest.Seq)
	}
}
