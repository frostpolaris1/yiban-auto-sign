package account

// 本人自助注销/撤销注销用例的单测。
//
// # 功能
// 钉住四条负向与两条正向：非 user 作用域、审计主体与作用域不一致、
// 已删除再删、仓储错误如实上抛；成功路径写 account.self_delete /
// account.self_restore 审计并只带 field=deleted_at。
// 本文件**不**连库、**不**发 HTTP；真库与 HTTP 面在 cmd/server。
//
// # 口径
// 归属判据只看 scope.UserID() 与 actor.UserID，二者必须相等；
// 7 天窗口以 core/account.SelfRestoreGrace 为唯一来源。

import (
	"context"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// selfRepo 记录自助端口收到的参数，并按需返回错误。
type selfRepo struct {
	Repo
	record        Record
	getErr        error
	softDeleteErr error
	softDeletes   int
	softDeleteBy  string
	restoreErr    error
	restores      int
	restoreBy     string
	notBefore     time.Time
}

func (f *selfRepo) GetIncludingDeleted(context.Context, corescope.Scope, string) (Record, error) {
	if f.getErr != nil {
		return Record{}, f.getErr
	}
	return f.record, nil
}

func (f *selfRepo) SoftDelete(_ context.Context, _ corescope.Scope, _ string, by string, _ time.Time) error {
	f.softDeletes++
	f.softDeleteBy = by
	return f.softDeleteErr
}

func (f *selfRepo) RestoreSelf(_ context.Context, _ corescope.Scope, _ string, by string, notBefore time.Time, _, _ int) (Record, error) {
	f.restores++
	f.restoreBy = by
	f.notBefore = notBefore
	if f.restoreErr != nil {
		return Record{}, f.restoreErr
	}
	return f.record, nil
}

type selfAudit struct {
	appaudit.Repo
	entries []coreaudit.Entry
}

func (f *selfAudit) Append(_ context.Context, in coreaudit.Entry) (coreaudit.Row, error) {
	f.entries = append(f.entries, in)
	return coreaudit.Row{}, nil
}

type selfTx struct{ app.TxManager }

func (selfTx) InTx(ctx context.Context, fn func(context.Context) error) error { return fn(ctx) }

var selfClock = clock.Fixed{T: time.Date(2026, 11, 4, 8, 0, 0, 0, time.UTC)}

func newSelfService(t *testing.T, repo *selfRepo, audits *selfAudit) *Service {
	t.Helper()
	svc, err := NewService(Deps{
		Accounts:   repo,
		Schools:    fakeSchoolReader{},
		Audit:      audits,
		Tx:         selfTx{},
		Clock:      selfClock,
		Cipher:     nil,
		Owner:      fakeOwnerReader{},
		Device:     &fakeDeviceReader{},
		Limit:      10,
		OwnerLimit: 10,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

func selfActor() appaudit.Actor {
	return appaudit.Actor{UserID: "u1", Role: coreauth.RoleUser, Label: "u***@example.com"}
}

func selfScope() corescope.Scope { return corescope.User("u1") }

// 非 user 作用域（如 master 全量）不得进入自助面：403 且不读仓储。
func TestSelfDeleteRejectsNonUserScope(t *testing.T) {
	repo := &selfRepo{}
	svc := newSelfService(t, repo, &selfAudit{})
	err := svc.SelfDelete(t.Context(), corescope.Master(), "a1", selfActor())
	if errs.From(err).Code != errs.CodeForbidden {
		t.Fatalf("code = %q, want forbidden", errs.From(err).Code)
	}
	if repo.softDeletes != 0 {
		t.Fatal("foreign scope reached the repo")
	}
}

// 审计主体与作用域不一致是调用方缺陷：拒绝且不写行。
func TestSelfDeleteRejectsActorMismatch(t *testing.T) {
	repo := &selfRepo{}
	svc := newSelfService(t, repo, &selfAudit{})
	actor := selfActor()
	actor.UserID = "u2"
	err := svc.SelfDelete(t.Context(), selfScope(), "a1", actor)
	if errs.From(err).Code != errs.CodeForbidden {
		t.Fatalf("code = %q, want forbidden", errs.From(err).Code)
	}
	if repo.softDeletes != 0 {
		t.Fatal("mismatched actor reached the repo")
	}
}

// 已删除再删 400：不重复软删、不写审计。
func TestSelfDeleteRejectsAlreadyDeleted(t *testing.T) {
	deletedAt := selfClock.T
	repo := &selfRepo{record: Record{ID: "a1", DeletedAt: &deletedAt}}
	audits := &selfAudit{}
	svc := newSelfService(t, repo, audits)
	err := svc.SelfDelete(t.Context(), selfScope(), "a1", selfActor())
	if errs.From(err).Code != errs.CodeValidation {
		t.Fatalf("code = %q, want validation_failed", errs.From(err).Code)
	}
	if repo.softDeletes != 0 || len(audits.entries) != 0 {
		t.Fatalf("already deleted: softDeletes=%d audits=%d", repo.softDeletes, len(audits.entries))
	}
}

// 成功路径：deleted_by 记本人，审计只带 field=deleted_at。
func TestSelfDeleteWritesAudit(t *testing.T) {
	repo := &selfRepo{record: Record{ID: "a1", SchoolID: "s1"}}
	audits := &selfAudit{}
	svc := newSelfService(t, repo, audits)
	if err := svc.SelfDelete(t.Context(), selfScope(), "a1", selfActor()); err != nil {
		t.Fatalf("SelfDelete: %v", err)
	}
	if repo.softDeleteBy != "u1" {
		t.Fatalf("deleted_by = %q, want u1", repo.softDeleteBy)
	}
	if len(audits.entries) != 1 {
		t.Fatalf("audit entries = %d, want 1", len(audits.entries))
	}
	entry := audits.entries[0]
	if entry.Action != coreaudit.ActionAccountSelfDelete || entry.TargetID != "a1" {
		t.Fatalf("audit entry = %+v", entry)
	}
	if len(entry.Detail) != 1 || entry.Detail["field"] != "deleted_at" {
		t.Fatalf("detail = %v, want only field=deleted_at", entry.Detail)
	}
}

// 撤销注销把 now-7d 当窗口下界交给仓储，并写 self_restore 审计。
func TestSelfRestorePassesGraceCutoff(t *testing.T) {
	repo := &selfRepo{record: Record{ID: "a1", SchoolID: "s1"}}
	audits := &selfAudit{}
	svc := newSelfService(t, repo, audits)
	if _, err := svc.SelfRestore(t.Context(), selfScope(), "a1", selfActor()); err != nil {
		t.Fatalf("SelfRestore: %v", err)
	}
	want := selfClock.T.Add(-coreaccount.SelfRestoreGrace)
	if !repo.notBefore.Equal(want) {
		t.Fatalf("notBefore = %s, want %s", repo.notBefore, want)
	}
	if repo.restoreBy != "u1" {
		t.Fatalf("deleted_by = %q, want u1", repo.restoreBy)
	}
	if len(audits.entries) != 1 || audits.entries[0].Action != coreaudit.ActionAccountSelfRestore {
		t.Fatalf("audit entries = %+v", audits.entries)
	}
}

// 仓储拒绝（管理员删除 403、超窗 409、已有生效账号 400）原样上抛，不写审计。
func TestSelfRestorePropagatesRepoError(t *testing.T) {
	repo := &selfRepo{restoreErr: errs.New(errs.CodeForbidden, "administrator deleted")}
	audits := &selfAudit{}
	svc := newSelfService(t, repo, audits)
	_, err := svc.SelfRestore(t.Context(), selfScope(), "a1", selfActor())
	if errs.From(err).Code != errs.CodeForbidden {
		t.Fatalf("code = %q, want forbidden", errs.From(err).Code)
	}
	if len(audits.entries) != 0 {
		t.Fatal("audit written after a failed restore")
	}
}

// 7 天是旧线 parity 的唯一来源：改动常量必须同时改旧线口径。
func TestSelfRestoreGraceIsSevenDays(t *testing.T) {
	if coreaccount.SelfRestoreGrace != 7*24*time.Hour {
		t.Fatalf("SelfRestoreGrace = %s, want 168h", coreaccount.SelfRestoreGrace)
	}
}
