package auth_test

// 登录账号自助注销/恢复的用例测试。
//
// # 功能
// 钉住六条不变式：二次口令三态（缺 400 / 错 403 / 连败 429）、
// master 不可自助注销、注销撤销全部会话、7 天宽限（边界内允许、
// 超窗统一失败）、不存在/口令错/超窗同一条 401、恢复不建立会话。
// 本文件**不**连数据库，**不**发 HTTP；真库与接线由 cmd/server e2e 负责。

import (
	"strings"
	"testing"
	"time"

	appauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/auth"
	appuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/user"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// seedSelfUser 把一行用户放进替身的活跃表。
func seedSelfUser(t *testing.T, h *harness, email, password string, role coreauth.Role) appuser.Record {
	t.Helper()
	hash, err := coreauth.HashPassword(password)
	if err != nil {
		t.Fatalf("hash password: %v", err)
	}
	h.users.nextID++
	rec := appuser.Record{
		ID:           "u-" + email,
		Email:        email,
		PasswordHash: hash,
		Role:         role,
		Status:       coreauth.StatusActive,
	}
	h.users.byEmail[email] = rec
	h.users.byID[rec.ID] = rec
	return rec
}

// markSelfDeleted 把替身里的活跃行移到软删表，模拟注销后的库内状态。
func markSelfDeleted(h *harness, rec appuser.Record, at time.Time) {
	deleted := rec
	deleted.DeletedAt = &at
	deleted.DeletedBy = rec.ID
	delete(h.users.byEmail, rec.Email)
	delete(h.users.byID, rec.ID)
	h.users.deletedByEmail[rec.Email] = deleted
	h.users.deletedByID[rec.ID] = deleted
}

func sessionOf(rec appuser.Record) coreauth.Session {
	return coreauth.Session{User: coreauth.User{
		ID: rec.ID, Email: rec.Email, Role: rec.Role, Status: rec.Status,
	}}
}

// TestDeleteSelfReauthGate 覆盖缺值/错误/连败三态与零业务写入；
// 错误与连败各留一条 reauth 审计，注销动作不落。
func TestDeleteSelfReauthGate(t *testing.T) {
	h := newHarness(t)
	user := seedSelfUser(t, h, "gate@example.com", "right-pass", coreauth.RoleUser)
	sess := sessionOf(user)

	if err := h.svc.DeleteSelf(t.Context(), sess, ""); errs.From(err).Code != errs.CodeValidation {
		t.Fatalf("missing password code = %v, want validation_failed", errs.From(err).Code)
	}
	if len(h.users.selfDeleted) != 0 || len(h.sessions.revokedAll) != 0 || len(h.audits.actions) != 0 {
		t.Fatal("a missing password wrote state")
	}

	if err := h.svc.DeleteSelf(t.Context(), sess, "wrong-pass"); errs.From(err).Code != errs.CodeForbidden {
		t.Fatalf("wrong password code = %v, want forbidden", errs.From(err).Code)
	}
	if len(h.users.selfDeleted) != 0 || len(h.sessions.revokedAll) != 0 {
		t.Fatal("a wrong password wrote business state")
	}
	// 失败本身要留痕：恰一条 reauth 审计，且不是注销动作。
	if len(h.audits.actions) != 1 || h.audits.actions[0] != coreaudit.ActionAuthReauthFailed {
		t.Fatalf("wrong-password audit = %v, want one auth.reauth_failed", h.audits.actions)
	}

	// 上面已有一次错误；补到阈值（共 5 次失败），下一次必须 429。
	for i := 1; i < appauth.ReauthFailureLimit; i++ {
		err := h.svc.DeleteSelf(t.Context(), sess, "wrong-pass")
		if errs.From(err).Code != errs.CodeForbidden {
			t.Fatalf("failure %d code = %v, want forbidden", i+1, errs.From(err).Code)
		}
	}
	err := h.svc.DeleteSelf(t.Context(), sess, "wrong-pass")
	if errs.From(err).Code != errs.CodeRateLimited {
		t.Fatalf("throttled code = %v, want rate_limited", errs.From(err).Code)
	}
	if len(h.users.selfDeleted) != 0 || len(h.sessions.revokedAll) != 0 {
		t.Fatal("a throttled call wrote business state")
	}
	// 5 次 403 + 1 次 429 各一条审计；没有任何 user.self_delete。
	if len(h.audits.actions) != appauth.ReauthFailureLimit+1 {
		t.Fatalf("reauth audit rows = %d, want %d", len(h.audits.actions), appauth.ReauthFailureLimit+1)
	}
	for _, action := range h.audits.actions {
		if action != coreaudit.ActionAuthReauthFailed {
			t.Fatalf("unexpected audit action %q", action)
		}
	}
}

// TestDeleteSelfRevokesSessionsAndAudits 覆盖成功路径：软删、撤销全部会话、
// 写 user.self_delete 审计。
func TestDeleteSelfRevokesSessionsAndAudits(t *testing.T) {
	h := newHarness(t)
	user := seedSelfUser(t, h, "delete@example.com", "right-pass", coreauth.RoleUser)

	if err := h.svc.DeleteSelf(t.Context(), sessionOf(user), "right-pass"); err != nil {
		t.Fatalf("DeleteSelf: %v", err)
	}
	if len(h.users.selfDeleted) != 1 || h.users.selfDeleted[0] != user.ID {
		t.Fatalf("self-deleted = %v, want [%s]", h.users.selfDeleted, user.ID)
	}
	if len(h.sessions.revokedAll) != 1 || h.sessions.revokedAll[0] != user.ID {
		t.Fatalf("revoked sessions = %v, want [%s]", h.sessions.revokedAll, user.ID)
	}
	if _, ok := h.users.byID[user.ID]; ok {
		t.Fatal("user is still live after self-delete")
	}
	if len(h.audits.actions) != 1 || h.audits.actions[0] != coreaudit.ActionUserSelfDelete {
		t.Fatalf("audit actions = %v, want one user.self_delete", h.audits.actions)
	}
}

// TestDeleteSelfRejectsMaster 覆盖管理员保护：master 口令正确也 403，零写入。
func TestDeleteSelfRejectsMaster(t *testing.T) {
	h := newHarness(t)
	boss := seedSelfUser(t, h, "boss@example.com", "right-pass", coreauth.RoleMaster)

	if err := h.svc.DeleteSelf(t.Context(), sessionOf(boss), "right-pass"); errs.From(err).Code != errs.CodeForbidden {
		t.Fatalf("master self-delete code = %v, want forbidden", errs.From(err).Code)
	}
	if len(h.users.selfDeleted) != 0 || len(h.sessions.revokedAll) != 0 || len(h.audits.actions) != 0 {
		t.Fatal("master self-delete wrote state")
	}
}

// TestRestoreSelfIndistinguishableFailures 覆盖不存在/超窗/口令错同一条 401，
// 且失败路径不写恢复、不写审计。
func TestRestoreSelfIndistinguishableFailures(t *testing.T) {
	h := newHarness(t)

	unknown := h.svc.RestoreSelf(t.Context(), "nobody@example.com", "pass")

	expired := seedSelfUser(t, h, "expired@example.com", "right-pass", coreauth.RoleUser)
	markSelfDeleted(h, expired, testClock.T.Add(-coreuser.SelfRestoreGrace-time.Second))
	expiredErr := h.svc.RestoreSelf(t.Context(), expired.Email, "right-pass")

	wrong := seedSelfUser(t, h, "wrong@example.com", "right-pass", coreauth.RoleUser)
	markSelfDeleted(h, wrong, testClock.T.Add(-time.Hour))
	wrongErr := h.svc.RestoreSelf(t.Context(), wrong.Email, "wrong-pass")

	adminDeleted := seedSelfUser(t, h, "admin-deleted@example.com", "right-pass", coreauth.RoleUser)
	markSelfDeleted(h, adminDeleted, testClock.T.Add(-time.Hour))
	adminRow := h.users.deletedByID[adminDeleted.ID]
	adminRow.DeletedBy = "admin-1"
	h.users.deletedByID[adminDeleted.ID] = adminRow
	h.users.deletedByEmail[adminDeleted.Email] = adminRow
	adminErr := h.svc.RestoreSelf(t.Context(), adminDeleted.Email, "right-pass")

	for name, err := range map[string]error{
		"unknown": unknown, "expired": expiredErr, "wrong": wrongErr, "admin-deleted": adminErr,
	} {
		if err != appauth.ErrRestoreFailed {
			t.Fatalf("%s error = %v, want ErrRestoreFailed", name, err)
		}
	}
	if len(h.users.restoredUsers) != 0 || len(h.users.restoredAccts) != 0 || len(h.audits.actions) != 0 {
		t.Fatal("a rejected restore wrote state")
	}
}

// TestRestoreSelfWithinGrace 覆盖 7 天边界内恢复：用户与账号一并恢复、
// 写 user.self_restore 审计、不建立会话。
func TestRestoreSelfWithinGrace(t *testing.T) {
	h := newHarness(t)
	user := seedSelfUser(t, h, "restore@example.com", "right-pass", coreauth.RoleUser)
	// 恰好 7 天前仍在窗内（判据是 > grace 才拒绝）。
	markSelfDeleted(h, user, testClock.T.Add(-coreuser.SelfRestoreGrace))

	if err := h.svc.RestoreSelf(t.Context(), strings.ToUpper(user.Email), "right-pass"); err != nil {
		t.Fatalf("RestoreSelf: %v", err)
	}
	if len(h.users.restoredUsers) != 1 || h.users.restoredUsers[0] != user.ID {
		t.Fatalf("restored users = %v, want [%s]", h.users.restoredUsers, user.ID)
	}
	if len(h.users.restoredAccts) != 1 || h.users.restoredAccts[0] != user.ID {
		t.Fatalf("restored accounts = %v, want owner %s", h.users.restoredAccts, user.ID)
	}
	if _, ok := h.users.byID[user.ID]; !ok {
		t.Fatal("user is not live after restore")
	}
	if len(h.audits.actions) != 1 || h.audits.actions[0] != coreaudit.ActionUserSelfRestore {
		t.Fatalf("audit actions = %v, want one user.self_restore", h.audits.actions)
	}
	if len(h.sessions.issued) != 0 {
		t.Fatal("restore established a session")
	}
}

// TestDeleteSelfUsesClock 钉住宽限时间戳来自注入时钟。
func TestDeleteSelfUsesClock(t *testing.T) {
	h := newHarness(t)
	user := seedSelfUser(t, h, "clock@example.com", "right-pass", coreauth.RoleUser)
	if err := h.svc.DeleteSelf(t.Context(), sessionOf(user), "right-pass"); err != nil {
		t.Fatalf("DeleteSelf: %v", err)
	}
	got := h.users.deletedByID[user.ID]
	if got.DeletedAt == nil || !got.DeletedAt.Equal(testClock.T) {
		t.Fatalf("deleted_at = %v, want %v", got.DeletedAt, testClock.T)
	}
}
