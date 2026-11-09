package settings_test

// 调度设置写回用例（sett2）的守卫单测。
//
// 判据：只有 master 能写；部分补丁只动列出的键；域校验拒绝时零写入
// 零审计；写入与 settings.schedule_writeback 审计同事务（审计失败回滚）；
// detail 只含变更键与旧→新值。

import (
	"context"
	"testing"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appsettings "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/settings"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// fakeScheduleStore 是内存覆盖行存取：int/bool 各一行位，
// 记录每次写与删。failWrite 注入写失败。
type fakeScheduleStore struct {
	intValue   int
	intExists  bool
	boolValue  bool
	boolExists bool
	intWrites  []int
	boolWrites []bool
	clears     []string
	writtenBy  string
	failWrite  bool
}

func (f *fakeScheduleStore) IntOverride(context.Context, string) (int, bool, error) {
	return f.intValue, f.intExists, nil
}

func (f *fakeScheduleStore) BoolOverride(context.Context, string) (bool, bool, error) {
	return f.boolValue, f.boolExists, nil
}

func (f *fakeScheduleStore) SetInt(_ context.Context, _ string, value int, by string) error {
	if f.failWrite {
		return errs.New(errs.CodeInternal, "write rejected")
	}
	f.intWrites = append(f.intWrites, value)
	f.intExists, f.intValue, f.writtenBy = true, value, by
	return nil
}

func (f *fakeScheduleStore) SetBool(_ context.Context, _ string, value bool, by string) error {
	if f.failWrite {
		return errs.New(errs.CodeInternal, "write rejected")
	}
	f.boolWrites = append(f.boolWrites, value)
	f.boolExists, f.boolValue, f.writtenBy = true, value, by
	return nil
}

func (f *fakeScheduleStore) ClearSetting(_ context.Context, key string) error {
	if f.failWrite {
		return errs.New(errs.CodeInternal, "write rejected")
	}
	f.clears = append(f.clears, key)
	switch key {
	case appsettings.KeyScheduleEdgeFrontSec, appsettings.KeyScheduleEdgeBackSec:
		f.intExists = false
	case appsettings.KeyScheduleAllowTimePref:
		f.boolExists = false
	}
	return nil
}

// rollbackTx 记录回调是否以错误收场：错误即真实事务回滚。
type rollbackTx struct {
	rolledBack bool
	committed  int
}

func (f *rollbackTx) InTx(ctx context.Context, fn func(context.Context) error) error {
	if err := fn(ctx); err != nil {
		f.rolledBack = true
		return err
	}
	f.committed++
	return nil
}

// scheduleActor 是写回审计主体（master）。
var scheduleActor = appaudit.Actor{UserID: "master-1", Role: coreauth.RoleMaster, Label: "m***@example.com"}

// newScheduleService 构造写回用例：两枚 edge 键域 [0,300]。
func newScheduleService(t *testing.T, store appsettings.ScheduleStore, audits *fakeAudit) (*appsettings.Service, *rollbackTx) {
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

// 只有 master 能写：admin 与 user 都 403，零写入零审计（角色负例）。
func TestScheduleSettingsWriteIsMasterOnly(t *testing.T) {
	store := &fakeScheduleStore{}
	svc, _ := newScheduleService(t, store, &fakeAudit{})
	changes := []appsettings.ScheduleChange{
		{Key: appsettings.KeyScheduleEdgeFrontSec, Op: appsettings.ScheduleOpSet, Value: 120},
	}
	for _, scope := range []corescope.Scope{corescope.Admin([]string{"school-1"}), corescope.User("u1")} {
		if err := svc.SetSchedule(context.Background(), scope, scheduleActor, changes); err == nil ||
			errs.From(err).Code != errs.CodeForbidden {
			t.Fatalf("scope %v err = %v, want forbidden", scope.Kind(), err)
		}
	}
	if len(store.intWrites) != 0 || len(store.clears) != 0 {
		t.Fatalf("被拒写到达存储: writes=%v clears=%v", store.intWrites, store.clears)
	}
}

// 成功写回：upsert 落行、恰一行审计、detail 只含变更键旧→新。
func TestScheduleSettingsWriteUpsertsAndAudits(t *testing.T) {
	store := &fakeScheduleStore{}
	audits := &fakeAudit{}
	svc, tx := newScheduleService(t, store, audits)

	err := svc.SetSchedule(context.Background(), corescope.Master(), scheduleActor, []appsettings.ScheduleChange{
		{Key: appsettings.KeyScheduleEdgeFrontSec, Op: appsettings.ScheduleOpSet, Value: 120},
	})
	if err != nil {
		t.Fatalf("SetSchedule: %v", err)
	}
	if len(store.intWrites) != 1 || store.intWrites[0] != 120 || store.writtenBy != "master-1" {
		t.Fatalf("store writes = %v by %q", store.intWrites, store.writtenBy)
	}
	if tx.committed != 1 {
		t.Fatalf("tx commits = %d, want 1", tx.committed)
	}
	if len(audits.entries) != 1 {
		t.Fatalf("audit rows = %d, want exactly 1", len(audits.entries))
	}
	entry := audits.entries[0]
	if entry.Action != coreaudit.ActionSettingsScheduleWriteback {
		t.Fatalf("action = %q, want settings.schedule_writeback", entry.Action)
	}
	if entry.TargetType != "settings" || entry.TargetID != "schedule" {
		t.Fatalf("target = %s/%s", entry.TargetType, entry.TargetID)
	}
	changes, ok := entry.Detail["changes"].(map[string][2]any)
	if !ok || len(changes) != 1 {
		t.Fatalf("detail changes = %#v", entry.Detail["changes"])
	}
	if pair := changes[appsettings.KeyScheduleEdgeFrontSec]; pair[0] != nil || pair[1] != 120 {
		t.Fatalf("pair = %#v, want [nil 120]", pair)
	}
}

// 旧行存在时 detail 记旧→新；布尔键同形。
func TestScheduleSettingsWriteRecordsOldValue(t *testing.T) {
	store := &fakeScheduleStore{intExists: true, intValue: 60, boolExists: true, boolValue: false}
	audits := &fakeAudit{}
	svc, _ := newScheduleService(t, store, audits)

	err := svc.SetSchedule(context.Background(), corescope.Master(), scheduleActor, []appsettings.ScheduleChange{
		{Key: appsettings.KeyScheduleEdgeBackSec, Op: appsettings.ScheduleOpSet, Value: 90},
		{Key: appsettings.KeyScheduleAllowTimePref, Op: appsettings.ScheduleOpSet, Value: true},
	})
	if err != nil {
		t.Fatalf("SetSchedule: %v", err)
	}
	changes := audits.entries[0].Detail["changes"].(map[string][2]any)
	if pair := changes[appsettings.KeyScheduleEdgeBackSec]; pair[0] != 60 || pair[1] != 90 {
		t.Fatalf("edge pair = %#v, want [60 90]", pair)
	}
	if pair := changes[appsettings.KeyScheduleAllowTimePref]; pair[0] != false || pair[1] != true {
		t.Fatalf("bool pair = %#v, want [false true]", pair)
	}
}

// 部分补丁语义：只列出的键被写，其余键零触碰（缺字段=不动）。
func TestScheduleSettingsPartialPatchLeavesOtherKeys(t *testing.T) {
	store := &fakeScheduleStore{intExists: true, intValue: 60}
	audits := &fakeAudit{}
	svc, _ := newScheduleService(t, store, audits)

	err := svc.SetSchedule(context.Background(), corescope.Master(), scheduleActor, []appsettings.ScheduleChange{
		{Key: appsettings.KeyScheduleAllowTimePref, Op: appsettings.ScheduleOpSet, Value: true},
	})
	if err != nil {
		t.Fatalf("SetSchedule: %v", err)
	}
	if len(store.intWrites) != 0 || len(store.boolWrites) != 1 {
		t.Fatalf("int writes = %v, bool writes = %v", store.intWrites, store.boolWrites)
	}
	if store.intExists != true || store.intValue != 60 {
		t.Fatalf("edge row changed: exists=%v value=%d", store.intExists, store.intValue)
	}
	changes := audits.entries[0].Detail["changes"].(map[string][2]any)
	if _, ok := changes[appsettings.KeyScheduleEdgeFrontSec]; ok {
		t.Fatal("未列出的键不得进 detail")
	}
}

// 显式删除：删行回 env；行缺失的删除是幂等成功（旧 nil→新 nil）。
func TestScheduleSettingsClearDeletesRow(t *testing.T) {
	store := &fakeScheduleStore{intExists: true, intValue: 60}
	audits := &fakeAudit{}
	svc, _ := newScheduleService(t, store, audits)

	err := svc.SetSchedule(context.Background(), corescope.Master(), scheduleActor, []appsettings.ScheduleChange{
		{Key: appsettings.KeyScheduleEdgeFrontSec, Op: appsettings.ScheduleOpClear},
	})
	if err != nil {
		t.Fatalf("SetSchedule: %v", err)
	}
	if len(store.clears) != 1 || store.intExists {
		t.Fatalf("clears = %v exists = %v", store.clears, store.intExists)
	}
	pair := audits.entries[0].Detail["changes"].(map[string][2]any)[appsettings.KeyScheduleEdgeFrontSec]
	if pair[0] != 60 || pair[1] != nil {
		t.Fatalf("clear pair = %#v, want [60 nil]", pair)
	}

	audits2 := &fakeAudit{}
	store2 := &fakeScheduleStore{}
	svc2, _ := newScheduleService(t, store2, audits2)
	if err := svc2.SetSchedule(context.Background(), corescope.Master(), scheduleActor, []appsettings.ScheduleChange{
		{Key: appsettings.KeyScheduleEdgeFrontSec, Op: appsettings.ScheduleOpClear},
	}); err != nil {
		t.Fatalf("idempotent clear: %v", err)
	}
	pair2 := audits2.entries[0].Detail["changes"].(map[string][2]any)[appsettings.KeyScheduleEdgeFrontSec]
	if pair2[0] != nil || pair2[1] != nil {
		t.Fatalf("absent clear pair = %#v, want [nil nil]", pair2)
	}
}

// 域校验负例：越域、类型错、未知键、带值删除、空更新——
// 全部 400 类拒绝且零写入零审计。
func TestScheduleSettingsRejectsInvalidChanges(t *testing.T) {
	store := &fakeScheduleStore{}
	audits := &fakeAudit{}
	svc, _ := newScheduleService(t, store, audits)
	ctx := context.Background()
	for _, changes := range [][]appsettings.ScheduleChange{
		{{Key: appsettings.KeyScheduleEdgeFrontSec, Op: appsettings.ScheduleOpSet, Value: 301}},
		{{Key: appsettings.KeyScheduleEdgeBackSec, Op: appsettings.ScheduleOpSet, Value: -1}},
		{{Key: appsettings.KeyScheduleEdgeFrontSec, Op: appsettings.ScheduleOpSet, Value: "true"}},
		{{Key: appsettings.KeyScheduleEdgeFrontSec, Op: appsettings.ScheduleOpSet, Value: 12.5}},
		{{Key: appsettings.KeyScheduleAllowTimePref, Op: appsettings.ScheduleOpSet, Value: 1}},
		{{Key: appsettings.KeyScheduleAllowTimePref, Op: appsettings.ScheduleOpSet, Value: "true"}},
		{{Key: "not.a.writable.key", Op: appsettings.ScheduleOpSet, Value: 1}},
		{{Key: appsettings.KeyScheduleEdgeFrontSec, Op: appsettings.ScheduleOpClear, Value: 60}},
		{},
	} {
		err := svc.SetSchedule(ctx, corescope.Master(), scheduleActor, changes)
		if err == nil || errs.From(err).Code != errs.CodeValidation {
			t.Fatalf("changes %#v err = %v, want validation", changes, err)
		}
	}
	if len(store.intWrites) != 0 || len(store.boolWrites) != 0 || len(store.clears) != 0 {
		t.Fatalf("被拒写到达存储: %v %v %v", store.intWrites, store.boolWrites, store.clears)
	}
	if len(audits.entries) != 0 {
		t.Fatalf("被拒写写了审计: %d rows", len(audits.entries))
	}
}

// 审计失败整体回滚：用例报错且事务回滚（真库同存亡由 pg e2e 钉）。
func TestScheduleSettingsAuditFailureRollsBack(t *testing.T) {
	store := &fakeScheduleStore{}
	audits := &fakeAudit{err: errs.New(errs.CodeInternal, "audit rejected")}
	svc, tx := newScheduleService(t, store, audits)

	err := svc.SetSchedule(context.Background(), corescope.Master(), scheduleActor, []appsettings.ScheduleChange{
		{Key: appsettings.KeyScheduleEdgeFrontSec, Op: appsettings.ScheduleOpSet, Value: 120},
	})
	if err == nil {
		t.Fatal("audit failure must fail the PUT")
	}
	if !tx.rolledBack {
		t.Fatal("audit failure must roll the transaction back")
	}
	if len(audits.entries) != 0 {
		t.Fatalf("audit rows = %d, want 0", len(audits.entries))
	}
}

// 未装配写面或解析器时失败关闭：不得静默当成功。
func TestScheduleSettingsFailsClosedWithoutStore(t *testing.T) {
	changes := []appsettings.ScheduleChange{
		{Key: appsettings.KeyScheduleEdgeFrontSec, Op: appsettings.ScheduleOpSet, Value: 120},
	}
	svcNoStore, _ := newAnnouncementService(t, &fakeStore{}, &fakeAudit{})
	if err := svcNoStore.SetSchedule(context.Background(), corescope.Master(), scheduleActor, changes); err == nil ||
		errs.From(err).Code != errs.CodeInternal {
		t.Fatalf("missing store err = %v, want internal", err)
	}
	tx := &rollbackTx{}
	svcNoResolver, err := appsettings.NewService(appsettings.Deps{
		Store: &fakeStore{}, Audit: &fakeAudit{}, Tx: tx, Clock: fixedClock{},
		Schedule: &fakeScheduleStore{},
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	if err := svcNoResolver.SetSchedule(context.Background(), corescope.Master(), scheduleActor, changes); err == nil ||
		errs.From(err).Code != errs.CodeInternal {
		t.Fatalf("missing resolver err = %v, want internal", err)
	}
}
