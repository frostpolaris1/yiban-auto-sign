package settings_test

// 配额写回用例（r4r）与解析链的守卫单测。
//
// 判据：只有 master 能写、master/admin 能读；0=不限哨兵是合法值；
// 越域/类型错/未知键/空更新拒绝时零写入零审计；写入与
// settings.quota_writeback 审计同事务（审计失败回滚）；
// 缺行=1（master 自用紧档），坏行回退缺省并每键 Warn 一次，
// 存储故障原样上抛（不当成不限）。

import (
	"bytes"
	"context"
	"log/slog"
	"strconv"
	"testing"

	appsettings "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/settings"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// fakeQuotaStore 是按键的内存覆盖行存取。
// 行值用原始字符串保存，坏行（非整数）由 IntOverride 报校验错，
// 与 pg 适配器同口径。
type fakeQuotaStore struct {
	rows      map[string]string
	writes    map[string]int
	clears    []string
	writtenBy string
	failWrite bool
	readErr   error
}

func newFakeQuotaStore() *fakeQuotaStore {
	return &fakeQuotaStore{rows: map[string]string{}, writes: map[string]int{}}
}

func (f *fakeQuotaStore) IntOverride(_ context.Context, key string) (int, bool, error) {
	if f.readErr != nil {
		return 0, false, f.readErr
	}
	raw, ok := f.rows[key]
	if !ok {
		return 0, false, nil
	}
	value, err := strconv.Atoi(raw)
	if err != nil {
		return 0, true, errs.New(errs.CodeValidation, "settings value is not an integer")
	}
	return value, true, nil
}

func (f *fakeQuotaStore) BoolOverride(context.Context, string) (bool, bool, error) {
	return false, false, nil
}

func (f *fakeQuotaStore) StringOverride(context.Context, string) (string, bool, error) {
	return "", false, nil
}

func (f *fakeQuotaStore) SetInt(_ context.Context, key string, value int, by string) error {
	if f.failWrite {
		return errs.New(errs.CodeInternal, "write rejected")
	}
	f.writes[key] = value
	f.rows[key] = strconv.Itoa(value)
	f.writtenBy = by
	return nil
}

func (f *fakeQuotaStore) SetBool(context.Context, string, bool, string) error { return nil }

func (f *fakeQuotaStore) SetString(context.Context, string, string, string) error { return nil }

func (f *fakeQuotaStore) ClearSetting(_ context.Context, key string) error {
	if f.failWrite {
		return errs.New(errs.CodeInternal, "write rejected")
	}
	f.clears = append(f.clears, key)
	delete(f.rows, key)
	return nil
}

// newQuotaService 构造配额用例：解析器带两枚 edge 域（NewResolver 要求）。
func newQuotaService(t *testing.T, store appsettings.ScheduleStore, audits *fakeAudit) (*appsettings.Service, *rollbackTx) {
	t.Helper()
	resolver, err := appsettings.NewResolver(store, map[string]appsettings.SettingDomain{
		appsettings.KeyScheduleEdgeFrontSec: {Min: 0, Max: 300},
		appsettings.KeyScheduleEdgeBackSec:  {Min: 0, Max: 300},
	}, nil)
	if err != nil {
		t.Fatalf("NewResolver: %v", err)
	}
	tx := &rollbackTx{}
	svc, err := appsettings.NewService(appsettings.Deps{
		Store: &fakeStore{}, Audit: audits, Tx: tx, Clock: fixedClock{},
		Schedule: store, Resolver: resolver,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc, tx
}

// 只有 master 能写：admin 与 user 都 403，零写入零审计。
func TestQuotaSettingsWriteIsMasterOnly(t *testing.T) {
	store := newFakeQuotaStore()
	audits := &fakeAudit{}
	svc, _ := newQuotaService(t, store, audits)
	changes := []appsettings.QuotaChange{
		{Key: appsettings.KeyQuotaMaxUsers, Op: appsettings.OpSet, Value: 5},
	}
	for _, scope := range []corescope.Scope{corescope.Admin([]string{"school-1"}), corescope.User("u1")} {
		if err := svc.SetQuota(context.Background(), scope, scheduleActor, changes); err == nil ||
			errs.From(err).Code != errs.CodeForbidden {
			t.Fatalf("scope %v err = %v, want forbidden", scope.Kind(), err)
		}
	}
	if len(store.writes) != 0 || len(store.clears) != 0 || len(audits.entries) != 0 {
		t.Fatalf("被拒写到达存储: writes=%v clears=%v audit=%d", store.writes, store.clears, len(audits.entries))
	}
}

// 成功写回：upsert 落行、恰一行审计、detail 只含变更键旧→新。
func TestQuotaSettingsWriteUpsertsAndAudits(t *testing.T) {
	store := newFakeQuotaStore()
	audits := &fakeAudit{}
	svc, tx := newQuotaService(t, store, audits)

	err := svc.SetQuota(context.Background(), corescope.Master(), scheduleActor, []appsettings.QuotaChange{
		{Key: appsettings.KeyQuotaMaxUsers, Op: appsettings.OpSet, Value: 5},
	})
	if err != nil {
		t.Fatalf("SetQuota: %v", err)
	}
	if store.writes[appsettings.KeyQuotaMaxUsers] != 5 || store.writtenBy != "master-1" {
		t.Fatalf("writes = %v by %q", store.writes, store.writtenBy)
	}
	if tx.committed != 1 {
		t.Fatalf("tx commits = %d, want 1", tx.committed)
	}
	if len(audits.entries) != 1 {
		t.Fatalf("audit rows = %d, want exactly 1", len(audits.entries))
	}
	entry := audits.entries[0]
	if entry.Action != coreaudit.ActionSettingsQuotaWriteback {
		t.Fatalf("action = %q, want settings.quota_writeback", entry.Action)
	}
	if entry.TargetType != "settings" || entry.TargetID != "quota" {
		t.Fatalf("target = %s/%s", entry.TargetType, entry.TargetID)
	}
	changes, ok := entry.Detail["changes"].(map[string][2]any)
	if !ok || len(changes) != 1 {
		t.Fatalf("detail changes = %#v", entry.Detail["changes"])
	}
	if pair := changes[appsettings.KeyQuotaMaxUsers]; pair[0] != nil || pair[1] != 5 {
		t.Fatalf("pair = %#v, want [nil 5]", pair)
	}
}

// 旧行存在时 detail 记旧→新；0 是合法新值（不限哨兵）。
func TestQuotaSettingsWriteRecordsOldValueAndAcceptsZero(t *testing.T) {
	store := newFakeQuotaStore()
	store.rows[appsettings.KeyQuotaMaxAccounts] = "3"
	audits := &fakeAudit{}
	svc, _ := newQuotaService(t, store, audits)

	err := svc.SetQuota(context.Background(), corescope.Master(), scheduleActor, []appsettings.QuotaChange{
		{Key: appsettings.KeyQuotaMaxAccounts, Op: appsettings.OpSet, Value: 0},
	})
	if err != nil {
		t.Fatalf("SetQuota: %v", err)
	}
	if store.writes[appsettings.KeyQuotaMaxAccounts] != 0 {
		t.Fatalf("write = %d, want 0", store.writes[appsettings.KeyQuotaMaxAccounts])
	}
	pair := audits.entries[0].Detail["changes"].(map[string][2]any)[appsettings.KeyQuotaMaxAccounts]
	if pair[0] != 3 || pair[1] != 0 {
		t.Fatalf("pair = %#v, want [3 0]", pair)
	}
}

// 显式删除：删行回缺省 1；旧值进 detail，新值为 nil。
func TestQuotaSettingsClearDeletesRow(t *testing.T) {
	store := newFakeQuotaStore()
	store.rows[appsettings.KeyQuotaMaxUsers] = "5"
	audits := &fakeAudit{}
	svc, _ := newQuotaService(t, store, audits)

	err := svc.SetQuota(context.Background(), corescope.Master(), scheduleActor, []appsettings.QuotaChange{
		{Key: appsettings.KeyQuotaMaxUsers, Op: appsettings.OpClear},
	})
	if err != nil {
		t.Fatalf("SetQuota: %v", err)
	}
	if len(store.clears) != 1 || store.clears[0] != appsettings.KeyQuotaMaxUsers {
		t.Fatalf("clears = %v", store.clears)
	}
	pair := audits.entries[0].Detail["changes"].(map[string][2]any)[appsettings.KeyQuotaMaxUsers]
	if pair[0] != 5 || pair[1] != nil {
		t.Fatalf("clear pair = %#v, want [5 nil]", pair)
	}
	// 删行后读面回缺省 1。
	report, err := svc.QuotaSettings(context.Background(), corescope.Master())
	if err != nil {
		t.Fatalf("QuotaSettings: %v", err)
	}
	if report.MaxUsers.Value != appsettings.DefaultQuotaMaxUsers || report.MaxUsers.Source != appsettings.QuotaSourceDefault {
		t.Fatalf("max_users = %+v, want default 1", report.MaxUsers)
	}
}

// 负例：越域、类型错、未知键、空更新、clear 带值一律 400 零写入零审计。
func TestQuotaSettingsRejectsInvalidChanges(t *testing.T) {
	cases := []struct {
		name    string
		changes []appsettings.QuotaChange
	}{
		{"negative", []appsettings.QuotaChange{{Key: appsettings.KeyQuotaMaxUsers, Op: appsettings.OpSet, Value: -1}}},
		{"above max", []appsettings.QuotaChange{{Key: appsettings.KeyQuotaMaxUsers, Op: appsettings.OpSet, Value: 100001}}},
		{"wrong type", []appsettings.QuotaChange{{Key: appsettings.KeyQuotaMaxUsers, Op: appsettings.OpSet, Value: "5"}}},
		{"unknown key", []appsettings.QuotaChange{{Key: "quota.max_things", Op: appsettings.OpSet, Value: 5}}},
		{"unknown op", []appsettings.QuotaChange{{Key: appsettings.KeyQuotaMaxUsers, Op: "delete", Value: 5}}},
		{"clear with value", []appsettings.QuotaChange{{Key: appsettings.KeyQuotaMaxUsers, Op: appsettings.OpClear, Value: 5}}},
		{"empty", nil},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			store := newFakeQuotaStore()
			audits := &fakeAudit{}
			svc, tx := newQuotaService(t, store, audits)
			err := svc.SetQuota(context.Background(), corescope.Master(), scheduleActor, tc.changes)
			if err == nil || errs.From(err).Code != errs.CodeValidation {
				t.Fatalf("err = %v, want validation_failed", err)
			}
			if len(store.writes) != 0 || len(store.clears) != 0 || len(audits.entries) != 0 || tx.calls != 0 {
				t.Fatalf("被拒写有副作用: writes=%v clears=%v audit=%d tx=%d",
					store.writes, store.clears, len(audits.entries), tx.calls)
			}
		})
	}
}

// 审计失败整体回滚：写已发生但事务以错误收场（真实事务会回滚）。
func TestQuotaSettingsAuditFailureRollsBack(t *testing.T) {
	store := newFakeQuotaStore()
	audits := &fakeAudit{err: errs.New(errs.CodeInternal, "audit down")}
	svc, tx := newQuotaService(t, store, audits)

	err := svc.SetQuota(context.Background(), corescope.Master(), scheduleActor, []appsettings.QuotaChange{
		{Key: appsettings.KeyQuotaMaxUsers, Op: appsettings.OpSet, Value: 5},
	})
	if err == nil {
		t.Fatal("SetQuota succeeded with a failing audit")
	}
	if !tx.rolledBack {
		t.Fatal("transaction did not roll back on audit failure")
	}
	if len(audits.entries) != 0 {
		t.Fatalf("audit entries = %d, want 0", len(audits.entries))
	}
}

// 读面：缺行 source=default 值 1；有行 source=db 值行值；admin 可读、user 403。
func TestQuotaSettingsReport(t *testing.T) {
	store := newFakeQuotaStore()
	svc, _ := newQuotaService(t, store, &fakeAudit{})

	report, err := svc.QuotaSettings(context.Background(), corescope.Master())
	if err != nil {
		t.Fatalf("QuotaSettings: %v", err)
	}
	if report.MaxUsers.Value != 1 || report.MaxUsers.Source != appsettings.QuotaSourceDefault ||
		report.MaxAccounts.Value != 1 || report.MaxAccounts.Source != appsettings.QuotaSourceDefault {
		t.Fatalf("default report = %+v", report)
	}

	store.rows[appsettings.KeyQuotaMaxUsers] = "5"
	store.rows[appsettings.KeyQuotaMaxAccounts] = "0"
	report, err = svc.QuotaSettings(context.Background(), corescope.Admin([]string{"school-1"}))
	if err != nil {
		t.Fatalf("QuotaSettings admin: %v", err)
	}
	if report.MaxUsers.Value != 5 || report.MaxUsers.Source != appsettings.QuotaSourceDB {
		t.Fatalf("max_users = %+v, want db 5", report.MaxUsers)
	}
	if report.MaxAccounts.Value != 0 || report.MaxAccounts.Source != appsettings.QuotaSourceDB {
		t.Fatalf("max_accounts = %+v, want db 0 (unlimited)", report.MaxAccounts)
	}

	if _, err := svc.QuotaSettings(context.Background(), corescope.User("u1")); err == nil ||
		errs.From(err).Code != errs.CodeForbidden {
		t.Fatalf("user read err = %v, want forbidden", err)
	}
}

// 解析链：缺行=1；0=不限；坏行/越域回缺省并每键 Warn 一次；存储故障上抛。
func TestResolveQuotaChain(t *testing.T) {
	var buf bytes.Buffer
	store := newFakeQuotaStore()
	resolver, err := appsettings.NewResolver(store, map[string]appsettings.SettingDomain{
		appsettings.KeyScheduleEdgeFrontSec: {Min: 0, Max: 300},
		appsettings.KeyScheduleEdgeBackSec:  {Min: 0, Max: 300},
	}, slog.New(slog.NewTextHandler(&buf, nil)))
	if err != nil {
		t.Fatalf("NewResolver: %v", err)
	}

	value, err := resolver.UserLimit(context.Background())
	if err != nil || value != 1 {
		t.Fatalf("missing row = %d, %v; want 1", value, err)
	}
	store.rows[appsettings.KeyQuotaMaxUsers] = "0"
	value, err = resolver.UserLimit(context.Background())
	if err != nil || value != 0 {
		t.Fatalf("zero row = %d, %v; want 0 (unlimited)", value, err)
	}
	store.rows[appsettings.KeyQuotaMaxUsers] = "100001"
	value, err = resolver.UserLimit(context.Background())
	if err != nil || value != 1 {
		t.Fatalf("out-of-domain row = %d, %v; want fallback 1", value, err)
	}
	if warnLines(&buf) != 1 {
		t.Fatalf("warn lines = %d, want 1 (once per key)", warnLines(&buf))
	}
	store.rows[appsettings.KeyQuotaMaxUsers] = "not-a-number"
	value, err = resolver.UserLimit(context.Background())
	if err != nil || value != 1 {
		t.Fatalf("bad row = %d, %v; want fallback 1", value, err)
	}
	if warnLines(&buf) != 1 {
		t.Fatalf("warn lines = %d, want still 1 (once per key)", warnLines(&buf))
	}

	store.readErr = errs.New(errs.CodeInternal, "db down")
	if _, err := resolver.AccountLimit(context.Background()); err == nil {
		t.Fatal("storage failure was swallowed; want an error")
	}
}
