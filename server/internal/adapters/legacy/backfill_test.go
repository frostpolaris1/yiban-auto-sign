package legacy

// time_prefs 补录的单元判据（2gk）。
//
// 模拟的真实用户动作：运维给 `backfill-time-prefs` 一份 P8-A 导出目录。
// 断言的不变式：
//   - dry-run 一次写调用都不发（不写库）；
//   - 一致映射写一条；同一用户多账号取值不一致时整用户跳过并列名；
//   - 坏行逐条拒绝并给原因；
//   - 空输入不报错、不写；
//   - 报告零明文手机号。
//
// 真库事务回滚由 cmd/cli 的 e2e 钉住（假库没有事务语义）。

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

// fixedNow 是补录时刻的固定值（写库时间戳与报告可比对）。
var fixedNow = time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)

// fakeResolver 按表返回归属用户；表外旧账号返回 unmapped。
type fakeResolver struct {
	owners map[int64]OwnerRef
	err    error
	calls  int
}

func (f *fakeResolver) ResolveOwner(_ context.Context, oldID int64) (OwnerRef, error) {
	f.calls++
	if f.err != nil {
		return OwnerRef{}, f.err
	}
	if ref, ok := f.owners[oldID]; ok {
		return ref, nil
	}
	return OwnerRef{Status: ResolveUnmapped}, nil
}

// fakeWriter 记录写调用次数与内容，不发 SQL。
type fakeWriter struct {
	calls   int
	got     []PrefWrite
	gotNow  time.Time
	changed int64
	err     error
}

func (f *fakeWriter) UpsertOffsets(_ context.Context, writes []PrefWrite, now time.Time) (int64, error) {
	f.calls++
	f.got = append([]PrefWrite(nil), writes...)
	f.gotNow = now
	if f.err != nil {
		return 0, f.err
	}
	if f.changed == 0 {
		return int64(len(writes)), nil
	}
	return f.changed, nil
}

// writeBackfillExport 写一份最小导出物：只含补录读的两张表。
func writeBackfillExport(t *testing.T, accounts, prefs []map[string]any) string {
	t.Helper()
	dir := t.TempDir()
	for name, rows := range map[string][]map[string]any{
		"accounts.jsonl":   accounts,
		"time_prefs.jsonl": prefs,
	} {
		var sb strings.Builder
		for _, row := range rows {
			sb.WriteString(CanonicalLine(row))
			sb.WriteString("\n")
		}
		if err := os.WriteFile(filepath.Join(dir, name), []byte(sb.String()), 0o600); err != nil {
			t.Fatalf("write %s: %v", name, err)
		}
	}
	return dir
}

// accountRow 造一行旧 accounts.jsonl（只含补录用到的列）。
func accountRow(id int64, phone string) map[string]any {
	num := strconv.FormatInt(id, 10)
	return map[string]any{"id": JSONNumber(num), "phone": phone, "name": "acct-" + num}
}

// prefRow 造一行旧 time_prefs.jsonl。slot 为非数字时按原样透传。
func prefRow(phone string, slot any) map[string]any {
	return map[string]any{"phone": phone, "slot_min": slot, "updated_at": "2026-01-01T00:00:00"}
}

// runBackfill 用给定表跑一次补录。
func runBackfill(t *testing.T, dir string, apply bool, owners map[int64]OwnerRef) (BackfillReport, *fakeWriter, error) {
	t.Helper()
	w := &fakeWriter{}
	report, err := Backfill(context.Background(), BackfillOptions{
		Dir:      dir,
		Apply:    apply,
		Resolver: &fakeResolver{owners: owners},
		Writer:   w,
		Now:      fixedNow,
	})
	return report, w, err
}

func TestBackfillDryRunMakesNoWriteCall(t *testing.T) {
	dir := writeBackfillExport(t,
		[]map[string]any{accountRow(1, "13800000001")},
		[]map[string]any{prefRow("13800000001", JSONNumber("30"))},
	)
	report, w, err := runBackfill(t, dir, false, map[int64]OwnerRef{
		1: {UserID: "u-1", Status: ResolveOK},
	})
	if err != nil {
		t.Fatalf("dry-run: %v", err)
	}
	if w.calls != 0 {
		t.Fatalf("dry-run 发了 %d 次写调用，want 0", w.calls)
	}
	if len(report.Writes) != 1 || report.Writes[0].UserID != "u-1" || report.Writes[0].OffsetMin != 30 {
		t.Fatalf("dry-run 计划 = %+v, want 1 条 u-1/30", report.Writes)
	}
	if report.RowsWritten != 0 {
		t.Fatalf("dry-run rows_written = %d, want 0", report.RowsWritten)
	}
}

func TestBackfillApplyWritesConsistentOffset(t *testing.T) {
	dir := writeBackfillExport(t,
		[]map[string]any{accountRow(1, "13800000001")},
		[]map[string]any{prefRow("13800000001", JSONNumber("30"))},
	)
	report, w, err := runBackfill(t, dir, true, map[int64]OwnerRef{
		1: {UserID: "u-1", Status: ResolveOK},
	})
	if err != nil {
		t.Fatalf("apply: %v", err)
	}
	if w.calls != 1 {
		t.Fatalf("写调用 = %d, want 1", w.calls)
	}
	if len(w.got) != 1 || w.got[0].UserID != "u-1" || w.got[0].OffsetMin != 30 {
		t.Fatalf("写入内容 = %+v", w.got)
	}
	if !w.gotNow.Equal(fixedNow) {
		t.Fatalf("写入时间 = %v, want %v", w.gotNow, fixedNow)
	}
	if report.RowsWritten != 1 {
		t.Fatalf("rows_written = %d, want 1", report.RowsWritten)
	}
}

func TestBackfillSkipsConflictingUserAndListsAccounts(t *testing.T) {
	dir := writeBackfillExport(t,
		[]map[string]any{accountRow(1, "13800000001"), accountRow(2, "13800000002")},
		[]map[string]any{
			prefRow("13800000001", JSONNumber("30")),
			prefRow("13800000002", JSONNumber("45")),
		},
	)
	report, w, err := runBackfill(t, dir, true, map[int64]OwnerRef{
		1: {UserID: "u-1", Status: ResolveOK},
		2: {UserID: "u-1", Status: ResolveOK},
	})
	if err != nil {
		t.Fatalf("conflict: %v", err)
	}
	if len(report.Writes) != 0 || w.calls != 0 {
		t.Fatalf("冲突用户被写了：writes=%+v calls=%d", report.Writes, w.calls)
	}
	if len(report.Conflicts) != 1 {
		t.Fatalf("conflicts = %+v, want 1 条", report.Conflicts)
	}
	c := report.Conflicts[0]
	if c.UserID != "u-1" || len(c.Offsets) != 2 || c.Offsets[0] != 30 || c.Offsets[1] != 45 {
		t.Fatalf("冲突内容 = %+v", c)
	}
	if len(c.Accounts) != 2 || c.Accounts[0] != "138****0001" || c.Accounts[1] != "138****0002" {
		t.Fatalf("冲突账号 = %v, want 两个掩码手机号", c.Accounts)
	}
}

func TestBackfillSameUserSameOffsetWritesOnce(t *testing.T) {
	dir := writeBackfillExport(t,
		[]map[string]any{accountRow(1, "13800000001"), accountRow(2, "13800000002")},
		[]map[string]any{
			prefRow("13800000001", JSONNumber("30")),
			prefRow("13800000002", JSONNumber("30")),
		},
	)
	report, w, err := runBackfill(t, dir, true, map[int64]OwnerRef{
		1: {UserID: "u-1", Status: ResolveOK},
		2: {UserID: "u-1", Status: ResolveOK},
	})
	if err != nil {
		t.Fatalf("same offset: %v", err)
	}
	if len(report.Writes) != 1 || report.Writes[0].OffsetMin != 30 {
		t.Fatalf("writes = %+v, want 1 条 30", report.Writes)
	}
	if len(w.got) != 1 {
		t.Fatalf("写调用内容 = %+v, want 1 条", w.got)
	}
	if len(report.Writes[0].Accounts) != 2 {
		t.Fatalf("写入账号清单 = %v, want 2 个", report.Writes[0].Accounts)
	}
}

func TestBackfillRejectsBadRows(t *testing.T) {
	accounts := []map[string]any{
		accountRow(1, "13800000001"), // slot 越界
		accountRow(2, "13800000002"), // 非 5 倍数
		accountRow(3, "13800000003"), // slot 非数字
		accountRow(4, "13800000004"), // 无 legacy_ids 映射
		accountRow(5, "13800000005"), // 落地账号已软删
	}
	prefs := []map[string]any{
		prefRow("13800000001", JSONNumber("1440")),
		prefRow("13800000002", JSONNumber("7")),
		prefRow("13800000003", "abc"),
		prefRow("13800000004", JSONNumber("30")),
		prefRow("13800000005", JSONNumber("30")),
		prefRow("", JSONNumber("30")),
		prefRow("13899999999", JSONNumber("30")),
		prefRow("13800000001", JSONNumber("20")), // 同号第二行：输入重复
	}
	dir := writeBackfillExport(t, accounts, prefs)
	report, w, err := runBackfill(t, dir, true, map[int64]OwnerRef{
		1: {UserID: "u-1", Status: ResolveOK},
		2: {UserID: "u-2", Status: ResolveOK},
		3: {UserID: "u-3", Status: ResolveOK},
		4: {Status: ResolveUnmapped},
		5: {Status: ResolveInactive},
	})
	if err != nil {
		t.Fatalf("bad rows: %v", err)
	}
	if w.calls != 0 || len(report.Writes) != 0 {
		t.Fatalf("坏行被写了：writes=%+v calls=%d", report.Writes, w.calls)
	}
	if len(report.Skips) != len(prefs) {
		t.Fatalf("skips = %d, want %d：%+v", len(report.Skips), len(prefs), report.Skips)
	}
	want := []string{
		SkipSlotOutOfRange,
		SkipSlotGrain,
		SkipSlotNotInteger,
		SkipNoMapping,
		SkipInactive,
		SkipMissingPhone,
		SkipNoAccount,
		SkipDuplicateInput,
	}
	for i, reason := range want {
		if report.Skips[i].Reason != reason {
			t.Fatalf("skip[%d].reason = %q, want %q（全表 %+v）", i, report.Skips[i].Reason, reason, report.Skips)
		}
	}
	if report.Skips[5].Phone != "" {
		t.Fatalf("空手机号行 phone = %q, want 空串", report.Skips[5].Phone)
	}
	if report.Skips[6].Phone != "138****9999" {
		t.Fatalf("孤儿行 phone = %q, want 掩码", report.Skips[6].Phone)
	}
}

func TestBackfillEmptyInputWritesNothing(t *testing.T) {
	dir := writeBackfillExport(t, nil, nil)
	report, w, err := runBackfill(t, dir, true, nil)
	if err != nil {
		t.Fatalf("empty input: %v", err)
	}
	if w.calls != 0 || len(report.Writes) != 0 || report.RowsWritten != 0 {
		t.Fatalf("空输入产生动作：writes=%+v calls=%d rows=%d", report.Writes, w.calls, report.RowsWritten)
	}
	if report.InputRows != 0 || report.AccountsRead != 0 {
		t.Fatalf("计数 = %d/%d, want 0/0", report.InputRows, report.AccountsRead)
	}
}

func TestBackfillMissingAccountsFileIsUsageError(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "time_prefs.jsonl"), nil, 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	_, _, err := runBackfill(t, dir, false, nil)
	var fatalErr *Error
	if !errors.As(err, &fatalErr) || fatalErr.Code != 2 {
		t.Fatalf("err = %v, want 退出码 2 的用法错误", err)
	}
}

func TestBackfillAccountsRowWithoutIDIsUsageError(t *testing.T) {
	dir := writeBackfillExport(t,
		[]map[string]any{{"phone": "13800000001", "name": "no-id"}},
		[]map[string]any{prefRow("13800000001", JSONNumber("30"))},
	)
	_, _, err := runBackfill(t, dir, false, nil)
	var fatalErr *Error
	if !errors.As(err, &fatalErr) || fatalErr.Code != 2 {
		t.Fatalf("err = %v, want 退出码 2 的用法错误", err)
	}
}

func TestBackfillReportHasNoPlaintextPhone(t *testing.T) {
	dir := writeBackfillExport(t,
		[]map[string]any{accountRow(1, "13800000001"), accountRow(2, "13800000002")},
		[]map[string]any{
			prefRow("13800000001", JSONNumber("30")),
			prefRow("13800000002", JSONNumber("45")),
			prefRow("13899999999", JSONNumber("30")),
		},
	)
	report, _, err := runBackfill(t, dir, false, map[int64]OwnerRef{
		1: {UserID: "u-1", Status: ResolveOK},
		2: {UserID: "u-1", Status: ResolveOK},
	})
	if err != nil {
		t.Fatalf("report: %v", err)
	}
	raw, err := json.Marshal(report)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	for _, phone := range []string{"13800000001", "13800000002", "13899999999"} {
		if strings.Contains(string(raw), phone) {
			t.Fatalf("报告含明文手机号 %s：%s", phone, raw)
		}
	}
}

func TestBackfillWriterErrorPropagates(t *testing.T) {
	dir := writeBackfillExport(t,
		[]map[string]any{accountRow(1, "13800000001")},
		[]map[string]any{prefRow("13800000001", JSONNumber("30"))},
	)
	w := &fakeWriter{err: errors.New("injected write failure")}
	_, err := Backfill(context.Background(), BackfillOptions{
		Dir:      dir,
		Apply:    true,
		Resolver: &fakeResolver{owners: map[int64]OwnerRef{1: {UserID: "u-1", Status: ResolveOK}}},
		Writer:   w,
		Now:      fixedNow,
	})
	if err == nil || !strings.Contains(err.Error(), "injected write failure") {
		t.Fatalf("err = %v, want 写失败上抛", err)
	}
}

func TestBackfillResolverErrorPropagates(t *testing.T) {
	dir := writeBackfillExport(t,
		[]map[string]any{accountRow(1, "13800000001")},
		[]map[string]any{prefRow("13800000001", JSONNumber("30"))},
	)
	_, err := Backfill(context.Background(), BackfillOptions{
		Dir:      dir,
		Resolver: &fakeResolver{err: errors.New("resolver down")},
		Writer:   &fakeWriter{},
		Now:      fixedNow,
	})
	if err == nil || !strings.Contains(err.Error(), "resolver down") {
		t.Fatalf("err = %v, want 解析失败上抛", err)
	}
}
