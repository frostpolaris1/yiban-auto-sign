package audit_test

// 巡检用例的结论映射。
//
// 口径：core 的 Status 是唯一事实；用例只做三件事——
// 定结论（ok / empty / broken）、把链头哈希转小写十六进制、盖巡检时刻。
// 空链不是通过（Empty=true, ok=false）；断链是结论，不是错误。

import (
	"context"
	"encoding/hex"
	"errors"
	"testing"
	"time"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
)

type fakeChain struct {
	status coreaudit.Status
	err    error
	called int
}

func (f *fakeChain) VerifyStatus(context.Context) (coreaudit.Status, error) {
	f.called++
	return f.status, f.err
}

var inspectAt = time.Date(2026, 11, 8, 9, 15, 30, 0, time.UTC)

func newInspector(t *testing.T, chain *fakeChain) *appaudit.Inspector {
	t.Helper()
	inspector, err := appaudit.NewInspector(chain, clock.Fixed{T: inspectAt})
	if err != nil {
		t.Fatalf("NewInspector: %v", err)
	}
	return inspector
}

func TestInspectorReportsIntactChain(t *testing.T) {
	last := make([]byte, 32)
	last[0] = 0xab
	last[31] = 0xcd
	chain := &fakeChain{status: coreaudit.Status{CheckedRows: 7, LastHash: last}}

	status, err := newInspector(t, chain).Status(context.Background())
	if err != nil {
		t.Fatalf("Status: %v", err)
	}
	if !status.OK || status.Empty || status.BrokenAt != nil {
		t.Fatalf("status = %+v", status)
	}
	if status.CheckedRows != 7 {
		t.Fatalf("checked rows = %d, want 7", status.CheckedRows)
	}
	if want := hex.EncodeToString(last); status.LastHash != want {
		t.Fatalf("last hash = %q, want %q", status.LastHash, want)
	}
	if !status.CheckedAt.Equal(inspectAt) {
		t.Fatalf("checked_at = %v, want %v", status.CheckedAt, inspectAt)
	}
}

func TestInspectorReportsBrokenChain(t *testing.T) {
	chain := &fakeChain{status: coreaudit.Status{CheckedRows: 42, BrokenAt: 42}}
	status, err := newInspector(t, chain).Status(context.Background())
	if err != nil {
		t.Fatalf("Status: %v", err)
	}
	if status.OK || status.Empty {
		t.Fatalf("status = %+v", status)
	}
	if status.BrokenAt == nil || *status.BrokenAt != 42 {
		t.Fatalf("broken_at = %v, want 42", status.BrokenAt)
	}
	if status.LastHash != "" {
		t.Fatalf("last hash = %q, want empty", status.LastHash)
	}
}

func TestInspectorReportsEmptyChain(t *testing.T) {
	chain := &fakeChain{status: coreaudit.Status{Empty: true}}
	status, err := newInspector(t, chain).Status(context.Background())
	if err != nil {
		t.Fatalf("Status: %v", err)
	}
	if status.OK {
		t.Fatal("an empty chain was reported intact; nothing was proven")
	}
	if !status.Empty {
		t.Fatal("empty was not surfaced")
	}
	if status.BrokenAt != nil || status.CheckedRows != 0 || status.LastHash != "" {
		t.Fatalf("status = %+v", status)
	}
}

func TestInspectorPropagatesReadError(t *testing.T) {
	sentinel := errors.New("read failed")
	_, err := newInspector(t, &fakeChain{err: sentinel}).Status(context.Background())
	if !errors.Is(err, sentinel) {
		t.Fatalf("err = %v, want the read error", err)
	}
}

func TestNewInspectorRejectsNilChain(t *testing.T) {
	if _, err := appaudit.NewInspector(nil, clock.Fixed{T: inspectAt}); err == nil {
		t.Fatal("a nil chain reader was accepted")
	}
}
