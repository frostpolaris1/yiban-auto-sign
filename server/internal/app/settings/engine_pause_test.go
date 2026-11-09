package settings_test

// 全局执行暂停用例（批 3a）的单测。
//
// 判据：方向分闸（暂停 master/admin、恢复仅 master，user 一律 403）、
// 幂等重放仍恰一行审计、审计 detail 只含字段与目标状态、
// 审计失败整体回滚、缺写端口失败关闭、读面缺行=false 故障上抛。

import (
	"context"
	"errors"
	"testing"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appsettings "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/settings"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// pauseStore 是覆盖行的内存替身：只服务 engine.paused 读写，
// 计数写入次数以判「被拒请求零写入」；readFail 注入读故障。
type pauseStore struct {
	boolRows map[string]string
	writes   int
	failWith error
	readFail error
}

func newPauseStore() *pauseStore {
	return &pauseStore{boolRows: map[string]string{}}
}

func (p *pauseStore) IntOverride(context.Context, string) (int, bool, error) {
	return 0, false, nil
}

func (p *pauseStore) BoolOverride(_ context.Context, key string) (bool, bool, error) {
	if p.readFail != nil {
		return false, false, p.readFail
	}
	raw, ok := p.boolRows[key]
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

func (p *pauseStore) SetBool(_ context.Context, key string, value bool, _ string) error {
	if p.failWith != nil {
		return p.failWith
	}
	p.writes++
	p.boolRows[key] = map[bool]string{true: "true", false: "false"}[value]
	return nil
}

func (p *pauseStore) SetInt(context.Context, string, int, string) error {
	return errs.New(errs.CodeInternal, "not used in pause tests")
}

func (p *pauseStore) ClearSetting(context.Context, string) error {
	return errs.New(errs.CodeInternal, "not used in pause tests")
}

// pauseFixture 装配全替身用例链。
type pauseFixture struct {
	svc    *appsettings.Service
	store  *pauseStore
	audits *fakeAudit
	tx     *rollbackTx
}

func newPauseFixture(t *testing.T, overrideFail error) *pauseFixture {
	t.Helper()
	store := newPauseStore()
	store.failWith = overrideFail
	audits := &fakeAudit{}
	tx := &rollbackTx{}
	svc, err := appsettings.NewService(appsettings.Deps{
		Store: &fakeStore{}, Audit: audits, Tx: tx, Clock: fixedClock{},
		Schedule: store,
		Resolver: mustResolver(t, store),
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return &pauseFixture{svc: svc, store: store, audits: audits, tx: tx}
}

func mustResolver(t *testing.T, store *pauseStore) *appsettings.Resolver {
	t.Helper()
	resolver, err := appsettings.NewResolver(store, map[string]appsettings.SettingDomain{
		appsettings.KeyScheduleEdgeFrontSec: {Min: 0, Max: 300},
		appsettings.KeyScheduleEdgeBackSec:  {Min: 0, Max: 300},
	}, nil)
	if err != nil {
		t.Fatalf("NewResolver: %v", err)
	}
	return resolver
}

// pauseActors 是三个角色的审计主体与作用域。
type pauseActors struct {
	master appaudit.Actor
	admin  appaudit.Actor
	user   appaudit.Actor
}

func newPauseActors() pauseActors {
	return pauseActors{
		master: appaudit.Actor{UserID: "m-1", Role: coreauth.RoleMaster, Label: "m***"},
		admin:  appaudit.Actor{UserID: "a-1", Role: coreauth.RoleAdmin, Label: "a***"},
		user:   appaudit.Actor{UserID: "u-1", Role: coreauth.RoleUser, Label: "u***"},
	}
}

// 暂停：master 与 admin 都成功；恢复：仅 master，admin 403，user 一律 403。
// 被拒请求零写入零审计。
func TestEnginePauseDirectionGates(t *testing.T) {
	actors := newPauseActors()

	// 暂停面：master 与 admin 皆可。
	for _, tc := range []struct {
		name  string
		actor appaudit.Actor
		scope corescope.Scope
	}{
		{"master 暂停", actors.master, corescope.Master()},
		{"admin 暂停", actors.admin, corescope.Admin(nil)},
	} {
		f := newPauseFixture(t, nil)
		if err := f.svc.SetEnginePaused(context.Background(), tc.scope, tc.actor, true); err != nil {
			t.Fatalf("%s: %v", tc.name, err)
		}
		if value := f.store.boolRows[appsettings.KeyEnginePaused]; value != "true" {
			t.Fatalf("%s: row = %q, want true", tc.name, value)
		}
		if len(f.audits.entries) != 1 {
			t.Fatalf("%s: audit rows = %d, want 1", tc.name, len(f.audits.entries))
		}
	}

	// 恢复面：admin 403，零写入零审计。
	f := newPauseFixture(t, nil)
	if err := f.svc.SetEnginePaused(context.Background(), corescope.Admin(nil), actors.admin, false); err == nil {
		t.Fatal("admin resume must be forbidden")
	} else if errs.From(err).Code != errs.CodeForbidden {
		t.Fatalf("admin resume code = %v, want forbidden", errs.From(err).Code)
	}
	if f.store.writes != 0 || len(f.audits.entries) != 0 {
		t.Fatalf("被拒恢复写了存储或审计: writes=%d audits=%d", f.store.writes, len(f.audits.entries))
	}

	// user：暂停与恢复都 403。
	for _, paused := range []bool{true, false} {
		scope := corescope.User("u-1")
		if err := f.svc.SetEnginePaused(context.Background(), scope, actors.user, paused); err == nil {
			t.Fatalf("user pause=%v must be forbidden", paused)
		}
	}
	if f.store.writes != 0 || len(f.audits.entries) != 0 {
		t.Fatalf("user 被拒请求写了存储或审计: writes=%d audits=%d", f.store.writes, len(f.audits.entries))
	}
}

// 恢复成功：master 写 false；幂等重放（重复暂停/重复恢复）每次仍恰一行审计。
func TestEnginePauseIdempotentReplay(t *testing.T) {
	actors := newPauseActors()
	f := newPauseFixture(t, nil)
	ctx := context.Background()

	if err := f.svc.SetEnginePaused(ctx, corescope.Master(), actors.master, true); err != nil {
		t.Fatalf("pause: %v", err)
	}
	if err := f.svc.SetEnginePaused(ctx, corescope.Master(), actors.master, true); err != nil {
		t.Fatalf("pause replay: %v", err)
	}
	if err := f.svc.SetEnginePaused(ctx, corescope.Master(), actors.master, false); err != nil {
		t.Fatalf("resume: %v", err)
	}
	if err := f.svc.SetEnginePaused(ctx, corescope.Master(), actors.master, false); err != nil {
		t.Fatalf("resume replay: %v", err)
	}
	// 四次成功各恰一行审计；行仍是恰一行 settings（upsert，不叠行）。
	if len(f.audits.entries) != 4 {
		t.Fatalf("audit rows = %d, want 4（每次成功恰一行）", len(f.audits.entries))
	}
	if len(f.store.boolRows) != 1 {
		t.Fatalf("settings rows = %d, want 1（upsert 不叠行）", len(f.store.boolRows))
	}
	if value := f.store.boolRows[appsettings.KeyEnginePaused]; value != "false" {
		t.Fatalf("row after resume = %q, want false", value)
	}
	// 恢复是写行不是删行：行仍存在（缺行也读 false，但本用例无删行语义）。
	if _, found, _ := f.store.BoolOverride(ctx, appsettings.KeyEnginePaused); !found {
		t.Fatal("resume must write the row false, not delete it")
	}
}

// 审计 detail 只含字段与目标状态；动作与目标位正确。
func TestEnginePauseAuditShape(t *testing.T) {
	actors := newPauseActors()
	f := newPauseFixture(t, nil)
	ctx := context.Background()
	if err := f.svc.SetEnginePaused(ctx, corescope.Master(), actors.master, true); err != nil {
		t.Fatalf("pause: %v", err)
	}
	entry := f.audits.entries[0]
	if entry.Action != coreaudit.ActionSettingsEnginePaused {
		t.Fatalf("action = %q", entry.Action)
	}
	if entry.TargetType != "settings" || entry.TargetID != "engine_paused" {
		t.Fatalf("target = %q/%q", entry.TargetType, entry.TargetID)
	}
	if entry.ActorUserID != actors.master.UserID || entry.ActorRole != actors.master.Role {
		t.Fatalf("actor = %q/%q", entry.ActorUserID, entry.ActorRole)
	}
	if len(entry.Detail) != 2 || entry.Detail["field"] != "engine_paused" || entry.Detail["to"] != "paused" {
		t.Fatalf("detail = %v, want {field,to} only", entry.Detail)
	}
	// detail 必须能过审计链白名单编码（负例形状防回退）。
	if _, err := coreaudit.EncodeDetail(entry.Detail); err != nil {
		t.Fatalf("detail rejected by the audit whitelist: %v", err)
	}

	// 恢复的 detail 目标态是 resumed。
	if err := f.svc.SetEnginePaused(ctx, corescope.Master(), actors.master, false); err != nil {
		t.Fatalf("resume: %v", err)
	}
	if to := f.audits.entries[1].Detail["to"]; to != "resumed" {
		t.Fatalf("resume detail to = %v, want resumed", to)
	}
}

// 审计失败整体回滚：事务以错误收场（真库的行级同存亡在 pg e2e 钉）。
func TestEnginePauseAuditFailureRollsBack(t *testing.T) {
	actors := newPauseActors()
	f := newPauseFixture(t, nil)
	f.audits.err = errors.New("audit rejected")
	if err := f.svc.SetEnginePaused(context.Background(), corescope.Master(), actors.master, true); err == nil {
		t.Fatal("pause must fail when the audit append fails")
	}
	if !f.tx.rolledBack {
		t.Fatal("audit failure must roll the transaction back")
	}
	if len(f.audits.entries) != 0 {
		t.Fatalf("audit rows = %d, want 0", len(f.audits.entries))
	}
}

// 装配缺写端口是编程错误：失败关闭（CodeInternal），零写入。
func TestEnginePauseFailsClosedWithoutStore(t *testing.T) {
	actors := newPauseActors()
	audits := &fakeAudit{}
	svc, err := appsettings.NewService(appsettings.Deps{
		Store: &fakeStore{}, Audit: audits, Tx: &fakeTx{}, Clock: fixedClock{},
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	if err := svc.SetEnginePaused(context.Background(), corescope.Master(), actors.master, true); err == nil {
		t.Fatal("pause without a store must fail closed")
	} else if errs.From(err).Code != errs.CodeInternal {
		t.Fatalf("code = %v, want internal", errs.From(err).Code)
	}
	if len(audits.entries) != 0 {
		t.Fatalf("失败关闭仍写了审计: %d", len(audits.entries))
	}
}

// 读面：缺行=false；行值生效；存储故障上抛；坏行按解析器口径回退 false。
func TestEnginePausedRead(t *testing.T) {
	actors := newPauseActors()
	ctx := context.Background()

	f := newPauseFixture(t, nil)
	paused, err := f.svc.EnginePaused(ctx)
	if err != nil || paused {
		t.Fatalf("missing row = (%v, %v), want (false, nil)", paused, err)
	}
	if err := f.svc.SetEnginePaused(ctx, corescope.Master(), actors.master, true); err != nil {
		t.Fatalf("pause: %v", err)
	}
	if paused, err = f.svc.EnginePaused(ctx); err != nil || !paused {
		t.Fatalf("after pause = (%v, %v), want (true, nil)", paused, err)
	}

	// 存储故障上抛：故障不是「未暂停」。
	broken := newPauseStore()
	broken.readFail = errors.New("db down")
	audits := &fakeAudit{}
	svc, err := appsettings.NewService(appsettings.Deps{
		Store: &fakeStore{}, Audit: audits, Tx: &fakeTx{}, Clock: fixedClock{},
		Schedule: broken, Resolver: mustResolver(t, broken),
	})
	if err != nil {
		t.Fatalf("NewService(broken): %v", err)
	}
	if _, err := svc.EnginePaused(ctx); err == nil {
		t.Fatal("store failure must propagate, not read as not-paused")
	}

	// 坏行：解析器口径回退 false 并出声（不是故障、不是崩溃）。
	bad := newPauseStore()
	bad.boolRows[appsettings.KeyEnginePaused] = "TRUE"
	svc, err = appsettings.NewService(appsettings.Deps{
		Store: &fakeStore{}, Audit: &fakeAudit{}, Tx: &fakeTx{}, Clock: fixedClock{},
		Schedule: bad, Resolver: mustResolver(t, bad),
	})
	if err != nil {
		t.Fatalf("NewService(bad): %v", err)
	}
	if paused, err := svc.EnginePaused(ctx); err != nil || paused {
		t.Fatalf("bad row = (%v, %v), want (false, nil)", paused, err)
	}
}
