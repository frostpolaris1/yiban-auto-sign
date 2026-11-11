package audit_test

// 巡检用例的结论映射。
//
// 口径：core 的 Status 是唯一事实；用例只做三件事——
// 定结论（ok / empty / broken）、把链头哈希转小写十六进制、盖巡检时刻。
// 空链不是通过（Empty=true, ok=false）；断链是结论，不是错误。

import (
	"bytes"
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
	anchor *coreaudit.Anchor
	meta   *coreaudit.AnchorMeta
}

func (f *fakeChain) VerifyStatus(_ context.Context, anchor *coreaudit.Anchor, meta *coreaudit.AnchorMeta) (coreaudit.Status, error) {
	f.called++
	f.anchor = anchor
	f.meta = meta
	return f.status, f.err
}

// fakeAnchorReader 返回固定锚点或错误。
type fakeAnchorReader struct {
	anchor *coreaudit.Anchor
	err    error
	calls  int
}

func (f *fakeAnchorReader) Latest(context.Context) (*coreaudit.Anchor, error) {
	f.calls++
	return f.anchor, f.err
}

// fakeAnchorMetaReader 返回固定库内高水位或错误。
type fakeAnchorMetaReader struct {
	meta  *coreaudit.AnchorMeta
	err   error
	calls int
}

func (f *fakeAnchorMetaReader) AnchorMeta(context.Context) (*coreaudit.AnchorMeta, error) {
	f.calls++
	return f.meta, f.err
}

// metaOf 用锚点造一条一致的库内高水位（三字段全等）。
func metaOf(anchor *coreaudit.Anchor) *coreaudit.AnchorMeta {
	return &coreaudit.AnchorMeta{Seq: anchor.Seq, HeadHash: anchor.Hash, LineHash: anchor.LineHash}
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

// ===== 库外锚点 =====

func TestInspectorAnchorDisabled(t *testing.T) {
	chain := &fakeChain{status: coreaudit.Status{CheckedRows: 3, LastID: 3}}
	status, err := newInspector(t, chain).Status(context.Background())
	if err != nil {
		t.Fatalf("Status: %v", err)
	}
	if !status.OK || status.AnchorStatus != appaudit.AnchorStatusDisabled {
		t.Fatalf("status = %+v, want ok with anchor disabled", status)
	}
	if chain.anchor != nil {
		t.Fatalf("chain received anchor %+v, want nil", chain.anchor)
	}
}

func TestInspectorAnchorMissingKeepsChainVerdict(t *testing.T) {
	chain := &fakeChain{status: coreaudit.Status{CheckedRows: 3, LastID: 3}}
	reader := &fakeAnchorReader{}
	status, err := newInspector(t, chain).WithAnchor(reader).
		WithAnchorMeta(&fakeAnchorMetaReader{}).Status(context.Background())
	if err != nil {
		t.Fatalf("Status: %v", err)
	}
	if !status.OK || status.AnchorStatus != appaudit.AnchorStatusMissing {
		t.Fatalf("status = %+v, want ok with anchor missing", status)
	}
	if chain.anchor != nil {
		t.Fatalf("chain received anchor %+v, want nil", chain.anchor)
	}
}

func TestInspectorAnchorOK(t *testing.T) {
	hash := make([]byte, 32)
	anchor := &coreaudit.Anchor{Seq: 3, Count: 3, Hash: hash}
	chain := &fakeChain{status: coreaudit.Status{
		CheckedRows: 3, LastID: 3, Anchor: coreaudit.AnchorResult{Checked: true, OK: true},
	}}
	status, err := newInspector(t, chain).WithAnchor(&fakeAnchorReader{anchor: anchor}).
		WithAnchorMeta(&fakeAnchorMetaReader{meta: metaOf(anchor)}).Status(context.Background())
	if err != nil {
		t.Fatalf("Status: %v", err)
	}
	if !status.OK || status.AnchorStatus != appaudit.AnchorStatusOK || !status.AnchorOK {
		t.Fatalf("status = %+v, want ok with anchor ok", status)
	}
	if status.AnchorSeq != 3 || status.AnchorCount != 3 {
		t.Fatalf("anchor seq/count = %d/%d, want 3/3", status.AnchorSeq, status.AnchorCount)
	}
	if chain.anchor != anchor {
		t.Fatalf("chain received anchor %+v, want the reader anchor", chain.anchor)
	}
}

func TestInspectorAnchorMismatchTurnsRed(t *testing.T) {
	hash := make([]byte, 32)
	anchor := &coreaudit.Anchor{Seq: 3, Count: 3, Hash: hash}
	chain := &fakeChain{status: coreaudit.Status{
		CheckedRows: 3, LastID: 3,
		Anchor: coreaudit.AnchorResult{Checked: true, OK: false, Reason: "prefix removed"},
	}}
	status, err := newInspector(t, chain).WithAnchor(&fakeAnchorReader{anchor: anchor}).
		WithAnchorMeta(&fakeAnchorMetaReader{meta: metaOf(anchor)}).Status(context.Background())
	if err != nil {
		t.Fatalf("Status: %v", err)
	}
	if status.OK || status.AnchorStatus != appaudit.AnchorStatusMismatch || status.AnchorOK {
		t.Fatalf("status = %+v, want red with anchor mismatch", status)
	}
	if status.AnchorReason != "prefix removed" {
		t.Fatalf("anchor reason = %q", status.AnchorReason)
	}
}

func TestInspectorAnchorReadErrorTurnsRed(t *testing.T) {
	chain := &fakeChain{status: coreaudit.Status{CheckedRows: 3, LastID: 3}}
	status, err := newInspector(t, chain).
		WithAnchor(&fakeAnchorReader{err: errors.New("permission denied")}).
		WithAnchorMeta(&fakeAnchorMetaReader{}).Status(context.Background())
	if err != nil {
		t.Fatalf("Status: %v", err)
	}
	if status.OK || status.AnchorStatus != appaudit.AnchorStatusError {
		t.Fatalf("status = %+v, want red with anchor error", status)
	}
	if chain.anchor != nil {
		t.Fatalf("chain received anchor %+v, want nil", chain.anchor)
	}
}

// 删锚点末行/整文件/文件最强低于 meta：库内高水位还在，必须判 anchor_truncated。
func TestInspectorAnchorTruncated(t *testing.T) {
	meta := &coreaudit.AnchorMeta{Seq: 3, HeadHash: make([]byte, 32), LineHash: make([]byte, 32)}
	cases := map[string]*fakeAnchorReader{
		"file missing":   {},
		"file empty":     {err: coreaudit.ErrAnchorEmpty},
		"seq below meta": {anchor: &coreaudit.Anchor{Seq: 2, Count: 2, Hash: make([]byte, 32), LineHash: make([]byte, 32)}},
	}
	// 链替身按 core 的口径返回失配结论：本测只钉 status.go 的状态映射。
	chainStatus := coreaudit.Status{CheckedRows: 3, LastID: 3,
		Anchor: coreaudit.AnchorResult{Checked: true, OK: false, Reason: "anchor_truncated"}}
	for name, reader := range cases {
		t.Run(name, func(t *testing.T) {
			chain := &fakeChain{status: chainStatus}
			status, err := newInspector(t, chain).WithAnchor(reader).
				WithAnchorMeta(&fakeAnchorMetaReader{meta: meta}).Status(context.Background())
			if err != nil {
				t.Fatalf("Status: %v", err)
			}
			if status.OK || status.AnchorStatus != appaudit.AnchorStatusMismatch {
				t.Fatalf("status = %+v, want red with anchor mismatch", status)
			}
			if status.AnchorReason != "anchor_truncated" {
				t.Fatalf("anchor reason = %q, want anchor_truncated", status.AnchorReason)
			}
		})
	}
}

// count 改写：文件行字节变了，LineHash 与 meta 不符（seq/head_hash 不变），
// 必须判 anchor_rewritten——只比链内哈希时它绕得过比对。
func TestInspectorAnchorCountRewrite(t *testing.T) {
	anchor := &coreaudit.Anchor{Seq: 3, Count: 3, Hash: make([]byte, 32), LineHash: bytes.Repeat([]byte{0x2a}, 32)}
	meta := &coreaudit.AnchorMeta{Seq: 3, HeadHash: anchor.Hash, LineHash: bytes.Repeat([]byte{0x2b}, 32)}
	chain := &fakeChain{status: coreaudit.Status{CheckedRows: 3, LastID: 3,
		Anchor: coreaudit.AnchorResult{Checked: true, OK: false, Reason: "anchor_rewritten"}}}
	status, err := newInspector(t, chain).WithAnchor(&fakeAnchorReader{anchor: anchor}).
		WithAnchorMeta(&fakeAnchorMetaReader{meta: meta}).Status(context.Background())
	if err != nil {
		t.Fatalf("Status: %v", err)
	}
	if status.OK || status.AnchorStatus != appaudit.AnchorStatusMismatch {
		t.Fatalf("status = %+v, want red with anchor mismatch", status)
	}
	if status.AnchorReason != "anchor_rewritten" {
		t.Fatalf("anchor reason = %q, want anchor_rewritten", status.AnchorReason)
	}
}

// 文件有锚点但库内没有高水位：无法检出截断，必须报错而不是放行。
func TestInspectorAnchorMetaMissing(t *testing.T) {
	anchor := &coreaudit.Anchor{Seq: 3, Count: 3, Hash: make([]byte, 32)}
	chain := &fakeChain{status: coreaudit.Status{CheckedRows: 3, LastID: 3}}
	status, err := newInspector(t, chain).WithAnchor(&fakeAnchorReader{anchor: anchor}).
		WithAnchorMeta(&fakeAnchorMetaReader{}).Status(context.Background())
	if err != nil {
		t.Fatalf("Status: %v", err)
	}
	if status.OK || status.AnchorStatus != appaudit.AnchorStatusError {
		t.Fatalf("status = %+v, want red with anchor error", status)
	}
	if status.AnchorReason != "anchor_meta_missing" {
		t.Fatalf("anchor reason = %q, want anchor_meta_missing", status.AnchorReason)
	}
}

// 文件比库内高水位更强：上一次 meta 写失败或有人追加未登记行，判红。
func TestInspectorAnchorUnrecorded(t *testing.T) {
	anchor := &coreaudit.Anchor{Seq: 4, Count: 4, Hash: make([]byte, 32)}
	meta := &coreaudit.AnchorMeta{Seq: 3, HeadHash: make([]byte, 32)}
	chain := &fakeChain{status: coreaudit.Status{CheckedRows: 4, LastID: 4,
		Anchor: coreaudit.AnchorResult{Checked: true, OK: false, Reason: "anchor_unrecorded"}}}
	status, err := newInspector(t, chain).WithAnchor(&fakeAnchorReader{anchor: anchor}).
		WithAnchorMeta(&fakeAnchorMetaReader{meta: meta}).Status(context.Background())
	if err != nil {
		t.Fatalf("Status: %v", err)
	}
	if status.OK || status.AnchorStatus != appaudit.AnchorStatusMismatch {
		t.Fatalf("status = %+v, want red with anchor mismatch", status)
	}
	if status.AnchorReason != "anchor_unrecorded" {
		t.Fatalf("anchor reason = %q, want anchor_unrecorded", status.AnchorReason)
	}
}

// 库内高水位读失败：不得当成"没有锚点"。
func TestInspectorAnchorMetaReadError(t *testing.T) {
	anchor := &coreaudit.Anchor{Seq: 3, Count: 3, Hash: make([]byte, 32)}
	chain := &fakeChain{status: coreaudit.Status{CheckedRows: 3, LastID: 3}}
	status, err := newInspector(t, chain).WithAnchor(&fakeAnchorReader{anchor: anchor}).
		WithAnchorMeta(&fakeAnchorMetaReader{err: errors.New("db down")}).Status(context.Background())
	if err != nil {
		t.Fatalf("Status: %v", err)
	}
	if status.OK || status.AnchorStatus != appaudit.AnchorStatusError {
		t.Fatalf("status = %+v, want red with anchor error", status)
	}
	if status.AnchorReason != "anchor_meta_read_failed" {
		t.Fatalf("anchor reason = %q, want anchor_meta_read_failed", status.AnchorReason)
	}
}

func TestInspectorAnchorNotCheckedOnBrokenChain(t *testing.T) {
	hash := make([]byte, 32)
	anchor := &coreaudit.Anchor{Seq: 3, Count: 3, Hash: hash}
	chain := &fakeChain{status: coreaudit.Status{CheckedRows: 2, BrokenAt: 3}}
	status, err := newInspector(t, chain).WithAnchor(&fakeAnchorReader{anchor: anchor}).
		WithAnchorMeta(&fakeAnchorMetaReader{meta: metaOf(anchor)}).Status(context.Background())
	if err != nil {
		t.Fatalf("Status: %v", err)
	}
	if status.OK || status.AnchorStatus != appaudit.AnchorStatusNotChecked {
		t.Fatalf("status = %+v, want broken with anchor not checked", status)
	}
}

// 空链带锚点：Empty 保留（页面文案），但 ok=false 且锚点判不符。
func TestInspectorAnchorMismatchOnEmptyChain(t *testing.T) {
	hash := make([]byte, 32)
	anchor := &coreaudit.Anchor{Seq: 3, Count: 3, Hash: hash}
	chain := &fakeChain{status: coreaudit.Status{
		Empty:  true,
		Anchor: coreaudit.AnchorResult{Checked: true, OK: false, Reason: "chain empty"},
	}}
	status, err := newInspector(t, chain).WithAnchor(&fakeAnchorReader{anchor: anchor}).
		WithAnchorMeta(&fakeAnchorMetaReader{meta: metaOf(anchor)}).Status(context.Background())
	if err != nil {
		t.Fatalf("Status: %v", err)
	}
	if status.OK || !status.Empty || status.AnchorStatus != appaudit.AnchorStatusMismatch {
		t.Fatalf("status = %+v, want empty + anchor mismatch + not ok", status)
	}
}
