package user

// 本人邮件开关用例的单测。
//
// # 功能
// 钉住本人约束、审计形状与错误透传：目标 user id 必须等于审计主体；
// 成功写入追加一条 user.mail_notify，detail 只含 field 与 on/off。
// 本文件**不**连库、**不**发 HTTP；缺省 true 的真库口径在 cmd/server。
//
// # 口径
// 开关只影响签到失败邮件；本用例不发信。

import (
	"context"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

// mailRepo 记录开关写入与读取，并支持注入错误。
type mailRepo struct {
	Repo
	enabled    bool
	writes     int
	writeErr   error
	readErr    error
	lastUserID string
}

func (f *mailRepo) MailNotify(_ context.Context, _ string) (bool, error) {
	if f.readErr != nil {
		return false, f.readErr
	}
	return f.enabled, nil
}

func (f *mailRepo) SetMailNotify(_ context.Context, id string, enabled bool) error {
	f.writes++
	f.lastUserID = id
	if f.writeErr != nil {
		return f.writeErr
	}
	f.enabled = enabled
	return nil
}

func newMailService(t *testing.T, repo *mailRepo, audits *fakeAudit) *Service {
	t.Helper()
	svc, err := NewService(Deps{
		Users:         repo,
		Audit:         audits,
		Tx:            fakeTx{},
		Clock:         clock.Fixed{T: time.Date(2026, 11, 4, 8, 0, 0, 0, time.UTC)},
		KDF:           coreauth.PlainHasher{},
		Limit:         10000,
		Sessions:      fakeIssuer{},
		OwnerAccounts: fakeCounter{},
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

func mailActor() audit.Actor {
	return audit.Actor{UserID: "u1", Role: coreauth.RoleUser, Label: "u***@example.com"}
}

// 成功写入：本人 id 落到仓储，审计只带 field 与 to=off。
func TestSetMailNotifyWritesAudit(t *testing.T) {
	repo := &mailRepo{enabled: true}
	audits := &fakeAudit{}
	svc := newMailService(t, repo, audits)
	enabled, err := svc.SetMailNotify(t.Context(), "u1", false, mailActor())
	if err != nil {
		t.Fatalf("SetMailNotify: %v", err)
	}
	if enabled || repo.enabled {
		t.Fatalf("enabled = %v, repo = %v, want false", enabled, repo.enabled)
	}
	if repo.lastUserID != "u1" {
		t.Fatalf("updated user = %q, want u1", repo.lastUserID)
	}
	if len(audits.entries) != 1 {
		t.Fatalf("audit entries = %d, want 1", len(audits.entries))
	}
	entry := audits.entries[0]
	if entry.Action != coreaudit.ActionUserMailNotify || entry.TargetID != "u1" {
		t.Fatalf("audit entry = %+v", entry)
	}
	if entry.Detail["field"] != "mail_notify" || entry.Detail["to"] != "off" {
		t.Fatalf("detail = %v, want field=mail_notify to=off", entry.Detail)
	}
}

// 他人 id 不得改：403 且不写行、不写审计。
func TestSetMailNotifyRejectsForeignUser(t *testing.T) {
	repo := &mailRepo{}
	audits := &fakeAudit{}
	svc := newMailService(t, repo, audits)
	_, err := svc.SetMailNotify(t.Context(), "u2", false, mailActor())
	if errs.From(err).Code != errs.CodeForbidden {
		t.Fatalf("code = %q, want forbidden", errs.From(err).Code)
	}
	if repo.writes != 0 || len(audits.entries) != 0 {
		t.Fatalf("writes=%d audits=%d, want 0/0", repo.writes, len(audits.entries))
	}
}

// 空用户 id 是未登录：401。
func TestMailNotifyRequiresUser(t *testing.T) {
	svc := newMailService(t, &mailRepo{}, &fakeAudit{})
	if _, err := svc.MailNotify(t.Context(), "  "); errs.From(err).Code != errs.CodeUnauthorized {
		t.Fatalf("code = %q, want unauthorized", errs.From(err).Code)
	}
}

// 仓储错误原样上抛，审计为零行。
func TestSetMailNotifyPropagatesRepoError(t *testing.T) {
	repo := &mailRepo{writeErr: errs.ErrNotFound}
	audits := &fakeAudit{}
	svc := newMailService(t, repo, audits)
	_, err := svc.SetMailNotify(t.Context(), "u1", true, mailActor())
	if errs.From(err).Code != errs.CodeNotFound {
		t.Fatalf("code = %q, want not_found", errs.From(err).Code)
	}
	if len(audits.entries) != 0 {
		t.Fatal("audit written after a failed update")
	}
}

// 读取直接返回仓储值（缺省 true 由数据库列默认值保证）。
func TestMailNotifyReadsRepo(t *testing.T) {
	svc := newMailService(t, &mailRepo{enabled: true}, &fakeAudit{})
	enabled, err := svc.MailNotify(t.Context(), "u1")
	if err != nil || !enabled {
		t.Fatalf("MailNotify = (%v, %v), want (true, nil)", enabled, err)
	}
}
