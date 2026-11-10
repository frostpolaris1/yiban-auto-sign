package settings_test

// 调度设置写回用例（sett2）的守卫单测。
//
// 判据：只有 master 能写；部分补丁只动列出的键；域校验拒绝时零写入
// 零审计；写入与 settings.schedule_writeback 审计同事务（审计失败回滚）；
// detail 只含变更键与旧→新值。

import (
	"context"
	"strconv"
	"testing"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appsettings "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/settings"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// fakeScheduleStore 是内存覆盖行存取：int/bool/string 各一行位，
// 记录每次写与删。failWrite 注入写失败。
type fakeScheduleStore struct {
	intValue   int
	intExists  bool
	boolValue  bool
	boolExists bool
	strValue   string
	strExists  bool
	intWrites  []int
	boolWrites []bool
	strWrites  []string
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

func (f *fakeScheduleStore) StringOverride(context.Context, string) (string, bool, error) {
	return f.strValue, f.strExists, nil
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

func (f *fakeScheduleStore) SetString(_ context.Context, _ string, value string, by string) error {
	if f.failWrite {
		return errs.New(errs.CodeInternal, "write rejected")
	}
	f.strWrites = append(f.strWrites, value)
	f.strExists, f.strValue, f.writtenBy = true, value, by
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
	case appsettings.KeyScheduleSignDist:
		f.strExists = false
	}
	return nil
}

// rollbackTx 记录回调是否以错误收场：错误即真实事务回滚。
type rollbackTx struct {
	rolledBack bool
	committed  int
	calls      int
}

func (f *rollbackTx) InTx(ctx context.Context, fn func(context.Context) error) error {
	f.calls++
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

// ===== 批 3b：四枚 DB-only 键进写面（周末门＋默认窗口对）=====

// keyedWriteStore 是按键区分行的写面替身：成对校验要对 start/end
// 两键读出不同生效值，fakeScheduleStore 的单行位不够用。
type keyedWriteStore struct {
	intRows    map[string]string
	boolRows   map[string]string
	strRows    map[string]string
	intWrites  map[string]int
	boolWrites map[string]bool
	strWrites  map[string]string
	clears     []string
	readErr    error
	failWrite  bool
}

func newKeyedWriteStore() *keyedWriteStore {
	return &keyedWriteStore{
		intRows: map[string]string{}, boolRows: map[string]string{}, strRows: map[string]string{},
		intWrites: map[string]int{}, boolWrites: map[string]bool{}, strWrites: map[string]string{},
	}
}

func (f *keyedWriteStore) IntOverride(_ context.Context, key string) (int, bool, error) {
	if f.readErr != nil {
		return 0, false, f.readErr
	}
	raw, ok := f.intRows[key]
	if !ok {
		return 0, false, nil
	}
	value, err := strconv.Atoi(raw)
	if err != nil {
		return 0, true, errs.New(errs.CodeValidation, "settings value is not an integer")
	}
	return value, true, nil
}

func (f *keyedWriteStore) BoolOverride(_ context.Context, key string) (bool, bool, error) {
	if f.readErr != nil {
		return false, false, f.readErr
	}
	raw, ok := f.boolRows[key]
	if !ok {
		return false, false, nil
	}
	switch raw {
	case "true":
		return true, true, nil
	case "false":
		return false, true, nil
	default:
		return false, true, errs.New(errs.CodeValidation, "settings value is not a boolean literal")
	}
}

func (f *keyedWriteStore) SetInt(_ context.Context, key string, value int, _ string) error {
	if f.failWrite {
		return errs.New(errs.CodeInternal, "write rejected")
	}
	if f.intRows == nil {
		f.intRows = map[string]string{}
	}
	if f.intWrites == nil {
		f.intWrites = map[string]int{}
	}
	f.intWrites[key] = value
	f.intRows[key] = strconv.Itoa(value)
	return nil
}

func (f *keyedWriteStore) SetBool(_ context.Context, key string, value bool, _ string) error {
	if f.failWrite {
		return errs.New(errs.CodeInternal, "write rejected")
	}
	if f.boolRows == nil {
		f.boolRows = map[string]string{}
	}
	if f.boolWrites == nil {
		f.boolWrites = map[string]bool{}
	}
	f.boolWrites[key] = value
	f.boolRows[key] = strconv.FormatBool(value)
	return nil
}

func (f *keyedWriteStore) StringOverride(_ context.Context, key string) (string, bool, error) {
	if f.readErr != nil {
		return "", false, f.readErr
	}
	raw, ok := f.strRows[key]
	return raw, ok, nil
}

func (f *keyedWriteStore) SetString(_ context.Context, key, value, _ string) error {
	if f.failWrite {
		return errs.New(errs.CodeInternal, "write rejected")
	}
	if f.strRows == nil {
		f.strRows = map[string]string{}
	}
	if f.strWrites == nil {
		f.strWrites = map[string]string{}
	}
	f.strWrites[key] = value
	f.strRows[key] = value
	return nil
}

func (f *keyedWriteStore) ClearSetting(_ context.Context, key string) error {
	if f.failWrite {
		return errs.New(errs.CodeInternal, "write rejected")
	}
	f.clears = append(f.clears, key)
	delete(f.intRows, key)
	delete(f.boolRows, key)
	delete(f.strRows, key)
	return nil
}

// 周末门键写回：set 落行、null 删行、audit detail 含 bool 旧→新；
// 旧三键不受影响（缺=不动）。
func TestScheduleSettingsWeekendKeysWriteAndClear(t *testing.T) {
	store := newKeyedWriteStore()
	audits := &fakeAudit{}
	svc, _ := newScheduleService(t, store, audits)
	ctx := context.Background()

	// set：true 落行，恰一行审计，changes 记 [nil true]。
	if err := svc.SetSchedule(ctx, corescope.Master(), scheduleActor, []appsettings.ScheduleChange{
		{Key: appsettings.KeyScheduleSundaySign, Op: appsettings.ScheduleOpSet, Value: true},
	}); err != nil {
		t.Fatalf("SetSchedule(sunday): %v", err)
	}
	if !store.boolWrites[appsettings.KeyScheduleSundaySign] {
		t.Fatalf("sunday row = %v", store.boolRows)
	}
	pair := audits.entries[0].Detail["changes"].(map[string][2]any)[appsettings.KeyScheduleSundaySign]
	if pair[0] != nil || pair[1] != true {
		t.Fatalf("sunday pair = %#v, want [nil true]", pair)
	}

	// 旧行存在时旧→新：false 覆盖 true。
	if err := svc.SetSchedule(ctx, corescope.Master(), scheduleActor, []appsettings.ScheduleChange{
		{Key: appsettings.KeyScheduleSaturdaySign, Op: appsettings.ScheduleOpSet, Value: false},
	}); err != nil {
		t.Fatalf("SetSchedule(saturday): %v", err)
	}
	pair = audits.entries[1].Detail["changes"].(map[string][2]any)[appsettings.KeyScheduleSaturdaySign]
	if pair[0] != nil || pair[1] != false {
		t.Fatalf("saturday pair = %#v, want [nil false]", pair)
	}

	// null 删行：ClearSetting 落到周末键；缺行=false 由读面口径保证。
	if err := svc.SetSchedule(ctx, corescope.Master(), scheduleActor, []appsettings.ScheduleChange{
		{Key: appsettings.KeyScheduleSundaySign, Op: appsettings.ScheduleOpClear},
	}); err != nil {
		t.Fatalf("SetSchedule(clear): %v", err)
	}
	if store.boolRows[appsettings.KeyScheduleSundaySign] != "" {
		t.Fatalf("sunday row survived clear: %q", store.boolRows[appsettings.KeyScheduleSundaySign])
	}
	value, found, err := store.BoolOverride(ctx, appsettings.KeyScheduleSundaySign)
	if err != nil || found || value {
		t.Fatalf("after clear BoolOverride = (%v, %v, %v), want missing row = false", value, found, err)
	}

	// 部分补丁不受扩字段影响：只动 allow_time_pref，周末键行零触碰。
	store2 := newKeyedWriteStore()
	store2.boolRows[appsettings.KeyScheduleSaturdaySign] = "true"
	audits2 := &fakeAudit{}
	svc2, _ := newScheduleService(t, store2, audits2)
	if err := svc2.SetSchedule(ctx, corescope.Master(), scheduleActor, []appsettings.ScheduleChange{
		{Key: appsettings.KeyScheduleAllowTimePref, Op: appsettings.ScheduleOpSet, Value: true},
	}); err != nil {
		t.Fatalf("SetSchedule(old key): %v", err)
	}
	if store2.boolRows[appsettings.KeyScheduleSaturdaySign] != "true" {
		t.Fatalf("weekend row changed by old-key patch: %v", store2.boolRows)
	}
	if _, ok := audits2.entries[0].Detail["changes"].(map[string][2]any)[appsettings.KeyScheduleSaturdaySign]; ok {
		t.Fatal("未列出的周末键不得进 detail")
	}

	// 3b 复审 F4：存量窗口**半对行**不得卡死其它键的写回——
	// 组合校验仅在本 PUT 触碰窗口键时执行（读面半对整对回退常量兜住）。
	store3 := newKeyedWriteStore()
	store3.intRows[appsettings.KeyScheduleDefaultSignStartMin] = "360" // 只有 start，end 缺行
	audits3 := &fakeAudit{}
	svc3, _ := newScheduleService(t, store3, audits3)
	if err := svc3.SetSchedule(ctx, corescope.Master(), scheduleActor, []appsettings.ScheduleChange{
		{Key: appsettings.KeyScheduleSundaySign, Op: appsettings.ScheduleOpSet, Value: true},
	}); err != nil {
		t.Fatalf("SetSchedule(sunday) 被存量半对窗口行卡住: %v", err)
	}
	if store3.boolRows[appsettings.KeyScheduleSundaySign] != "true" {
		t.Fatalf("sunday 未落行: %v", store3.boolRows)
	}
}

// 默认窗口对：成对写入成功；单独改一侧与另一侧生效值组合后
// 仍需 start<end（缺行回退常量参与组合），违规 400 零写零审计。
func TestScheduleSettingsSignWindowPairValidation(t *testing.T) {
	ctx := context.Background()
	builtinStart := coreschedule.DefaultSignWindowStartMinute
	builtinEnd := coreschedule.DefaultSignWindowEndMinute

	// 成对写入（无行）：两行都落，恰一行审计。
	store := newKeyedWriteStore()
	audits := &fakeAudit{}
	svc, _ := newScheduleService(t, store, audits)
	if err := svc.SetSchedule(ctx, corescope.Master(), scheduleActor, []appsettings.ScheduleChange{
		{Key: appsettings.KeyScheduleDefaultSignStartMin, Op: appsettings.ScheduleOpSet, Value: 480},
		{Key: appsettings.KeyScheduleDefaultSignEndMin, Op: appsettings.ScheduleOpSet, Value: 540},
	}); err != nil {
		t.Fatalf("SetSchedule(pair): %v", err)
	}
	if store.intWrites[appsettings.KeyScheduleDefaultSignStartMin] != 480 ||
		store.intWrites[appsettings.KeyScheduleDefaultSignEndMin] != 540 {
		t.Fatalf("pair rows = %v", store.intWrites)
	}
	if len(audits.entries) != 1 {
		t.Fatalf("audit rows = %d, want 1", len(audits.entries))
	}

	// 负例表：非法域值与组合违规都 400 类拒绝，零写零审计。
	negatives := []struct {
		name    string
		store   *keyedWriteStore
		changes []appsettings.ScheduleChange
	}{
		{
			name:  "start above domain",
			store: newKeyedWriteStore(),
			changes: []appsettings.ScheduleChange{
				{Key: appsettings.KeyScheduleDefaultSignStartMin, Op: appsettings.ScheduleOpSet, Value: 1440},
				{Key: appsettings.KeyScheduleDefaultSignEndMin, Op: appsettings.ScheduleOpSet, Value: 1500},
			},
		},
		{
			name:  "negative minute",
			store: newKeyedWriteStore(),
			changes: []appsettings.ScheduleChange{
				{Key: appsettings.KeyScheduleDefaultSignStartMin, Op: appsettings.ScheduleOpSet, Value: -1},
				{Key: appsettings.KeyScheduleDefaultSignEndMin, Op: appsettings.ScheduleOpSet, Value: 500},
			},
		},
		{
			name:  "lone start over builtin end",
			store: newKeyedWriteStore(),
			changes: []appsettings.ScheduleChange{
				{Key: appsettings.KeyScheduleDefaultSignStartMin, Op: appsettings.ScheduleOpSet, Value: 600},
			},
		},
		{
			name:  "lone end below builtin start",
			store: newKeyedWriteStore(),
			changes: []appsettings.ScheduleChange{
				{Key: appsettings.KeyScheduleDefaultSignEndMin, Op: appsettings.ScheduleOpSet, Value: 300},
			},
		},
		{
			name: "start crosses stored end",
			store: func() *keyedWriteStore {
				s := newKeyedWriteStore()
				s.intRows[appsettings.KeyScheduleDefaultSignEndMin] = "400"
				return s
			}(),
			changes: []appsettings.ScheduleChange{
				{Key: appsettings.KeyScheduleDefaultSignStartMin, Op: appsettings.ScheduleOpSet, Value: 600},
			},
		},
		{
			name: "end crosses stored start",
			store: func() *keyedWriteStore {
				s := newKeyedWriteStore()
				s.intRows[appsettings.KeyScheduleDefaultSignStartMin] = "500"
				return s
			}(),
			changes: []appsettings.ScheduleChange{
				{Key: appsettings.KeyScheduleDefaultSignEndMin, Op: appsettings.ScheduleOpSet, Value: 400},
			},
		},
	}
	for _, negative := range negatives {
		auditsNeg := &fakeAudit{}
		svcNeg, _ := newScheduleService(t, negative.store, auditsNeg)
		err := svcNeg.SetSchedule(ctx, corescope.Master(), scheduleActor, negative.changes)
		if err == nil || errs.From(err).Code != errs.CodeValidation {
			t.Fatalf("%s: err = %v, want validation", negative.name, err)
		}
		if len(negative.store.intWrites) != 0 || len(negative.store.boolWrites) != 0 || len(negative.store.clears) != 0 {
			t.Fatalf("%s: 被拒写到达存储: %v", negative.name, negative.store.intWrites)
		}
		if len(auditsNeg.entries) != 0 {
			t.Fatalf("%s: 被拒写写了审计: %d rows", negative.name, len(auditsNeg.entries))
		}
	}

	// 组合正例：DB end=400 时 start=350 单独改成功（350<400）。
	storeOK := newKeyedWriteStore()
	storeOK.intRows[appsettings.KeyScheduleDefaultSignEndMin] = "400"
	svcOK, _ := newScheduleService(t, storeOK, &fakeAudit{})
	if err := svcOK.SetSchedule(ctx, corescope.Master(), scheduleActor, []appsettings.ScheduleChange{
		{Key: appsettings.KeyScheduleDefaultSignStartMin, Op: appsettings.ScheduleOpSet, Value: 350},
	}); err != nil {
		t.Fatalf("single start within stored end: %v", err)
	}
	if storeOK.intWrites[appsettings.KeyScheduleDefaultSignStartMin] != 350 {
		t.Fatalf("start row = %v", storeOK.intWrites)
	}

	// 删除一侧不挡路（组合 300<470 通过）；删除后行消失。
	storeClear := newKeyedWriteStore()
	storeClear.intRows[appsettings.KeyScheduleDefaultSignStartMin] = "300"
	svcClear, _ := newScheduleService(t, storeClear, &fakeAudit{})
	if err := svcClear.SetSchedule(ctx, corescope.Master(), scheduleActor, []appsettings.ScheduleChange{
		{Key: appsettings.KeyScheduleDefaultSignEndMin, Op: appsettings.ScheduleOpClear},
	}); err != nil {
		t.Fatalf("clear end with stored start: %v", err)
	}
	if _, found, _ := storeClear.IntOverride(ctx, appsettings.KeyScheduleDefaultSignEndMin); found {
		t.Fatal("end row must be gone after clear")
	}

	// 语义对照（防常量漂移）：常量对本身合法（start<end 且在域内）。
	if builtinStart >= builtinEnd || builtinStart < 0 || builtinEnd > 1439 {
		t.Fatalf("builtin pair (%d, %d) must be a valid pair", builtinStart, builtinEnd)
	}
}

// 扩字段写回与审计同事务：审计失败整体回滚（新键同口径）。
func TestScheduleSettingsWeekendKeysAuditFailureRollsBack(t *testing.T) {
	store := newKeyedWriteStore()
	audits := &fakeAudit{err: errs.New(errs.CodeInternal, "audit rejected")}
	svc, tx := newScheduleService(t, store, audits)

	err := svc.SetSchedule(context.Background(), corescope.Master(), scheduleActor, []appsettings.ScheduleChange{
		{Key: appsettings.KeyScheduleSundaySign, Op: appsettings.ScheduleOpSet, Value: true},
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

// ===== 批 3c：铺点分布档进写面（schedule.sign_dist）=====
//
// 判据：只收 SignDist 强类型且值在小写词表内；set 落行、null 删行
//（回默认档 front）；audit detail 只含该键的旧→新字符串；非法值/错类型
// 400 零写零审计；审计失败整体回滚（既有同事务口径覆盖新键）。

// 分布档写回：set 落行并记旧→新；再写覆盖旧值；null 删行回默认档。
func TestScheduleSettingsSignDistWriteAndClear(t *testing.T) {
	ctx := context.Background()
	store := newKeyedWriteStore()
	audits := &fakeAudit{}
	svc, tx := newScheduleService(t, store, audits)

	// 首次 set：旧值 nil（缺行=默认档），新值 uniform。
	if err := svc.SetSchedule(ctx, corescope.Master(), scheduleActor, []appsettings.ScheduleChange{
		{Key: appsettings.KeyScheduleSignDist, Op: appsettings.ScheduleOpSet, Value: appsettings.SignDistUniform},
	}); err != nil {
		t.Fatalf("SetSchedule(uniform): %v", err)
	}
	if store.strRows[appsettings.KeyScheduleSignDist] != "uniform" {
		t.Fatalf("sign_dist row = %q, want uniform", store.strRows[appsettings.KeyScheduleSignDist])
	}
	if len(audits.entries) != 1 {
		t.Fatalf("audit rows = %d, want exactly 1", len(audits.entries))
	}
	pair := audits.entries[0].Detail["changes"].(map[string][2]any)[appsettings.KeyScheduleSignDist]
	if pair[0] != nil || pair[1] != "uniform" {
		t.Fatalf("pair = %#v, want [nil uniform]", pair)
	}
	if tx.committed != 1 {
		t.Fatalf("commits = %d, want 1", tx.committed)
	}

	// 覆盖：旧值读自覆盖行，detail 记 uniform→front。
	if err := svc.SetSchedule(ctx, corescope.Master(), scheduleActor, []appsettings.ScheduleChange{
		{Key: appsettings.KeyScheduleSignDist, Op: appsettings.ScheduleOpSet, Value: appsettings.SignDistFront},
	}); err != nil {
		t.Fatalf("SetSchedule(front): %v", err)
	}
	pair = audits.entries[1].Detail["changes"].(map[string][2]any)[appsettings.KeyScheduleSignDist]
	if pair[0] != "uniform" || pair[1] != "front" {
		t.Fatalf("pair = %#v, want [uniform front]", pair)
	}

	// null 删行：行缺失读回默认档 front。
	if err := svc.SetSchedule(ctx, corescope.Master(), scheduleActor, []appsettings.ScheduleChange{
		{Key: appsettings.KeyScheduleSignDist, Op: appsettings.ScheduleOpClear},
	}); err != nil {
		t.Fatalf("SetSchedule(clear): %v", err)
	}
	if _, found, err := store.StringOverride(ctx, appsettings.KeyScheduleSignDist); err != nil || found {
		t.Fatalf("after clear StringOverride found = %v (err %v), want missing row", found, err)
	}
	pair = audits.entries[2].Detail["changes"].(map[string][2]any)[appsettings.KeyScheduleSignDist]
	if pair[0] != "front" || pair[1] != nil {
		t.Fatalf("clear pair = %#v, want [front nil]", pair)
	}
}

// 非法词表/大小写/空串与错 JSON 类型一律 400 零写零审计。
func TestScheduleSettingsSignDistRejectsInvalidValues(t *testing.T) {
	cases := []struct {
		name  string
		value any
	}{
		{"uppercase", appsettings.SignDist("FRONT")},
		{"unknown", appsettings.SignDist("spread")},
		{"empty", appsettings.SignDist("")},
		{"raw_string", "uniform"},
		{"boolean", true},
		{"integer", 1},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			store := newKeyedWriteStore()
			audits := &fakeAudit{}
			svc, tx := newScheduleService(t, store, audits)
			err := svc.SetSchedule(context.Background(), corescope.Master(), scheduleActor,
				[]appsettings.ScheduleChange{
					{Key: appsettings.KeyScheduleSignDist, Op: appsettings.ScheduleOpSet, Value: c.value},
				})
			if err == nil || errs.From(err).Code != errs.CodeValidation {
				t.Fatalf("err = %v, want validation", err)
			}
			if len(store.strWrites) != 0 || len(audits.entries) != 0 || tx.committed != 0 {
				t.Fatalf("zero-write violated: writes=%v audits=%d commits=%d",
					store.strWrites, len(audits.entries), tx.committed)
			}
		})
	}
}

// 审计失败整体回滚：新字符串键与既有键同存亡。
func TestScheduleSettingsSignDistAuditFailureRollsBack(t *testing.T) {
	store := newKeyedWriteStore()
	audits := &fakeAudit{err: errs.New(errs.CodeInternal, "audit rejected")}
	svc, tx := newScheduleService(t, store, audits)

	err := svc.SetSchedule(context.Background(), corescope.Master(), scheduleActor,
		[]appsettings.ScheduleChange{
			{Key: appsettings.KeyScheduleSignDist, Op: appsettings.ScheduleOpSet, Value: appsettings.SignDistUniform},
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
