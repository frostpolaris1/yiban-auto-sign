package main

// AOG e2e：被拒账号编辑重提与改绑手机号回待审。
//
// # 功能
// 钉住四条用户可见的性质：
//   1. 被拒账号编辑后回 pending，驳回理由与审核戳清空，并进入待审队列。
//   2. 改绑手机号把非 pending 账号送回 pending，审核通过前不参与签到。
//   3. 仅改口令不改变 active、pending；credential_error 仍恢复 active。
//   4. 改绑撞号返回 409，原状态与原号不变，且不留重提审计。
//
// # 归属
// cmd/server 可同时引用 adapters 与 entry；本文件用真实 PostgreSQL。
//
// # 口径
// 缺 DSN 时由 w2cNewEnv 打可见跳过横幅，跳过不是绿。
// 每个用例独占一个 schema。手机号只取登记段。

import (
	"net/http"
	"testing"
	"time"

	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

// aogAccount 是本文件断言用的响应字段。
type aogAccount struct {
	ID           string     `json:"id"`
	PhoneMasked  string     `json:"phone_masked"`
	Status       string     `json:"status"`
	RejectReason string     `json:"reject_reason"`
	ReviewedAt   *time.Time `json:"reviewed_at"`
	ReviewedBy   *string    `json:"reviewed_by"`
}

// aogSetAccount 直接改账号状态与审核戳，用来造出各状态的前置条件。
func aogSetAccount(t *testing.T, e *w2cEnv, id, status, reason, reviewerID string) {
	t.Helper()
	var err error
	if reviewerID != "" {
		_, err = e.db.Exec(e.ctx, `
			UPDATE accounts
			   SET status = $2::account_status, reject_reason = $3,
			       reviewed_at = now(), reviewed_by = $4::uuid
			 WHERE id = $1::uuid`, id, status, reason, reviewerID)
	} else {
		_, err = e.db.Exec(e.ctx, `
			UPDATE accounts
			   SET status = $2::account_status, reject_reason = $3,
			       reviewed_at = NULL, reviewed_by = NULL
			 WHERE id = $1::uuid`, id, status, reason)
	}
	if err != nil {
		t.Fatalf("set account %s to %s: %v", id, status, err)
	}
}

// aogPatch 发一次 PATCH 并解出账号。
// 全部用例都编辑凭据面字段，统一补上二次口令。
func aogPatch(t *testing.T, e *w2cEnv, id string, body map[string]any, issued *coreauth.Issued) aogAccount {
	t.Helper()
	if _, ok := body["current_password"]; !ok {
		body["current_password"] = w2cSeedPassword
	}
	rec := e.do(http.MethodPatch, "/api/accounts/"+id, body, issued)
	w2cWantStatus(t, rec, http.StatusOK)
	var got aogAccount
	e.decode(rec, &got)
	return got
}

// aogCount 数某动作命中该账号的审计行。
func aogCount(t *testing.T, e *w2cEnv, action, id string) int64 {
	t.Helper()
	return e.scalarInt(
		"SELECT count(*) FROM audit_logs WHERE action = $1 AND target_id = $2", action, id)
}

func aogReview(t *testing.T, e *w2cEnv, id string, issued *coreauth.Issued) aogAccount {
	t.Helper()
	rec := e.do(http.MethodPost, "/api/accounts/"+id+"/review",
		map[string]any{"decision": "approve"}, issued)
	w2cWantStatus(t, rec, http.StatusOK)
	var got aogAccount
	e.decode(rec, &got)
	return got
}

// 被拒账号编辑一次口令：回 pending、清理由与审核戳、进待审队列，
// 管理员随后能按正常审核流程通过。
func TestAOGRejectedEditResubmits(t *testing.T) {
	e := w2cNewEnv(t)
	owner := e.user("aog-owner@example.com", coreauth.RoleUser)
	admin := e.user("aog-admin@example.com", coreauth.RoleAdmin)
	school := e.school("aog-rejected")
	e.bind(admin.ID, school.ID)
	id := e.account(school.ID, owner.ID, "13800000040")
	aogSetAccount(t, e, id, string(coreaccount.StatusRejected), "照片不清晰", admin.ID)

	got := aogPatch(t, e, id, map[string]any{"password": "new-secret"}, e.login(owner))
	if got.Status != string(coreaccount.StatusPending) {
		t.Fatalf("status = %q, want pending after an edit of a rejected account", got.Status)
	}
	if got.RejectReason != "" {
		t.Fatalf("reject_reason = %q, want empty after resubmit", got.RejectReason)
	}
	if got.ReviewedAt != nil || got.ReviewedBy != nil {
		t.Fatalf("review stamp survived the resubmit: at=%v by=%v", got.ReviewedAt, got.ReviewedBy)
	}

	// 进入待审队列：管理员按 status=pending 能读到本行。
	rec := e.do(http.MethodGet, "/api/accounts?status=pending", nil, e.login(admin))
	w2cWantStatus(t, rec, http.StatusOK)
	var page w2cAccountPage
	e.decode(rec, &page)
	found := false
	for _, item := range page.Items {
		if item.ID == id {
			found = true
		}
	}
	if !found {
		t.Fatalf("resubmitted account %s is absent from the pending queue", id)
	}

	// 队列里的行能走完审核：approve 后 active。
	if approved := aogReview(t, e, id, e.login(admin)); approved.Status != string(coreaccount.StatusActive) {
		t.Fatalf("status after review = %q, want active", approved.Status)
	}

	if n := aogCount(t, e, "account.update", id); n != 1 {
		t.Fatalf("account.update rows = %d, want 1", n)
	}
	if n := aogCount(t, e, "account.resubmit", id); n != 1 {
		t.Fatalf("account.resubmit rows = %d, want 1", n)
	}
	detail := e.scalarString(
		"SELECT detail FROM audit_logs WHERE action = 'account.resubmit' AND target_id = $1", id)
	if want := `{"field":"status","from":"rejected","to":"pending"}`; detail != want {
		t.Fatalf("resubmit detail = %s, want %s", detail, want)
	}
}

// 改绑手机号把 active 账号送回 pending；审核通过后才重新 active。
func TestAOGRebindReturnsActiveToPending(t *testing.T) {
	e := w2cNewEnv(t)
	owner := e.user("aog-rebind@example.com", coreauth.RoleUser)
	admin := e.user("aog-rebind-admin@example.com", coreauth.RoleAdmin)
	school := e.school("aog-rebind")
	e.bind(admin.ID, school.ID)
	id := e.account(school.ID, owner.ID, "13800000041")
	aogSetAccount(t, e, id, string(coreaccount.StatusActive), "", admin.ID)

	got := aogPatch(t, e, id, map[string]any{"phone": "13800000050"}, e.login(owner))
	if got.Status != string(coreaccount.StatusPending) {
		t.Fatalf("status = %q, want pending after a phone change", got.Status)
	}
	if got.PhoneMasked != "138****0050" {
		t.Fatalf("phone_masked = %q, want the new number", got.PhoneMasked)
	}
	if got.RejectReason != "" || got.ReviewedAt != nil || got.ReviewedBy != nil {
		t.Fatalf("rebind kept review metadata: %+v", got)
	}
	if phone := e.scalarString("SELECT phone FROM accounts WHERE id = $1::uuid", id); phone != "13800000050" {
		t.Fatalf("stored phone = %q, want the new number", phone)
	}
	if n := aogCount(t, e, "account.resubmit", id); n != 1 {
		t.Fatalf("account.resubmit rows = %d, want 1", n)
	}

	if approved := aogReview(t, e, id, e.login(admin)); approved.Status != string(coreaccount.StatusActive) {
		t.Fatalf("status after review = %q, want active", approved.Status)
	}
}

// 改绑优先于凭据恢复：credential_error 账号换号后进待审，不是直接 active。
// 只改口令时仍按恢复处理；同值手机号不是凭据变更，三条路径互斥。
func TestAOGCredentialErrorRebindBeatsRecovery(t *testing.T) {
	e := w2cNewEnv(t)
	owner := e.user("aog-cred@example.com", coreauth.RoleUser)
	admin := e.user("aog-cred-admin@example.com", coreauth.RoleAdmin)
	school := e.school("aog-cred")
	e.bind(admin.ID, school.ID)
	rebindID := e.account(school.ID, owner.ID, "13800000040")
	passwordID := e.account(school.ID, owner.ID, "13800000041")
	sameID := e.account(school.ID, owner.ID, "13800000060")
	for _, id := range []string{rebindID, passwordID, sameID} {
		aogSetAccount(t, e, id, string(coreaccount.StatusCredentialError), "", admin.ID)
	}

	rebound := aogPatch(t, e, rebindID, map[string]any{"phone": "13800000050"}, e.login(owner))
	if rebound.Status != string(coreaccount.StatusPending) {
		t.Fatalf("rebind from credential_error = %q, want pending", rebound.Status)
	}
	if n := aogCount(t, e, "account.credential_recovered", rebindID); n != 0 {
		t.Fatalf("rebind wrote %d recovery rows; want 0", n)
	}

	recovered := aogPatch(t, e, passwordID, map[string]any{"password": "new-secret"}, e.login(owner))
	if recovered.Status != string(coreaccount.StatusActive) {
		t.Fatalf("password edit from credential_error = %q, want active", recovered.Status)
	}
	if n := aogCount(t, e, "account.credential_recovered", passwordID); n != 1 {
		t.Fatalf("password edit wrote %d recovery rows; want 1", n)
	}
	if n := aogCount(t, e, "account.resubmit", passwordID); n != 0 {
		t.Fatalf("password edit wrote %d resubmit rows; want 0", n)
	}

	// 同值手机号没有换号，也没有改口令：状态与审计都不动。
	same := aogPatch(t, e, sameID, map[string]any{"phone": "13800000060"}, e.login(owner))
	if same.Status != string(coreaccount.StatusCredentialError) {
		t.Fatalf("unchanged phone on credential_error = %q, want credential_error", same.Status)
	}
	if n := aogCount(t, e, "account.credential_recovered", sameID); n != 0 {
		t.Fatalf("unchanged phone wrote %d recovery rows; want 0", n)
	}
	if n := aogCount(t, e, "account.resubmit", sameID); n != 0 {
		t.Fatalf("unchanged phone wrote %d resubmit rows; want 0", n)
	}
}

// 仅改口令不动状态：active 保持 active，pending 保持 pending，都不留重提审计。
func TestAOGPasswordEditKeepsStatus(t *testing.T) {
	e := w2cNewEnv(t)
	owner := e.user("aog-keep@example.com", coreauth.RoleUser)
	admin := e.user("aog-keep-admin@example.com", coreauth.RoleAdmin)
	school := e.school("aog-keep")
	e.bind(admin.ID, school.ID)
	activeID := e.account(school.ID, owner.ID, "13800000040")
	pendingID := e.account(school.ID, owner.ID, "13800000041")
	aogSetAccount(t, e, activeID, string(coreaccount.StatusActive), "", admin.ID)
	// pendingID 保持建号时的 pending。

	if got := aogPatch(t, e, activeID, map[string]any{"password": "pw-1"}, e.login(owner)); got.Status != string(coreaccount.StatusActive) {
		t.Fatalf("active account after a password edit = %q, want active", got.Status)
	}
	if got := aogPatch(t, e, pendingID, map[string]any{"password": "pw-2"}, e.login(owner)); got.Status != string(coreaccount.StatusPending) {
		t.Fatalf("pending account after a password edit = %q, want pending", got.Status)
	}
	for _, id := range []string{activeID, pendingID} {
		if n := aogCount(t, e, "account.resubmit", id); n != 0 {
			t.Fatalf("password edit left %d resubmit rows; want 0", n)
		}
	}
}

// 已处于 pending 的账号改绑手机号仍停在待审：不写 from=pending 的重提审计。
func TestAOGPendingRebindStaysQueued(t *testing.T) {
	e := w2cNewEnv(t)
	owner := e.user("aog-pending@example.com", coreauth.RoleUser)
	school := e.school("aog-pending")
	id := e.account(school.ID, owner.ID, "13800000040")

	got := aogPatch(t, e, id, map[string]any{"phone": "13800000050"}, e.login(owner))
	if got.Status != string(coreaccount.StatusPending) {
		t.Fatalf("status = %q, want pending", got.Status)
	}
	if n := aogCount(t, e, "account.resubmit", id); n != 0 {
		t.Fatalf("pending rebind wrote %d resubmit rows; want 0", n)
	}
}

// 管理员改绑用户的 active 账号同样回 pending；管理员编辑被拒账号也重提。
// 唯一队列是 pending，评审只收 pending，管理员没有第二条放行路径。
func TestAOGAdminEditsResubmit(t *testing.T) {
	e := w2cNewEnv(t)
	owner := e.user("aog-admin-edit@example.com", coreauth.RoleUser)
	admin := e.user("aog-admin-editor@example.com", coreauth.RoleAdmin)
	school := e.school("aog-admin-edit")
	e.bind(admin.ID, school.ID)
	rebindID := e.account(school.ID, owner.ID, "13800000040")
	rejectedID := e.account(school.ID, owner.ID, "13800000041")
	aogSetAccount(t, e, rebindID, string(coreaccount.StatusActive), "", admin.ID)
	aogSetAccount(t, e, rejectedID, string(coreaccount.StatusRejected), "资料不全", admin.ID)

	if got := aogPatch(t, e, rebindID, map[string]any{"phone": "13800000050"}, e.login(admin)); got.Status != string(coreaccount.StatusPending) {
		t.Fatalf("admin rebind = %q, want pending", got.Status)
	}
	if got := aogPatch(t, e, rejectedID, map[string]any{"password": "fixed-secret"}, e.login(admin)); got.Status != string(coreaccount.StatusPending) {
		t.Fatalf("admin edit of a rejected account = %q, want pending", got.Status)
	}
	for _, id := range []string{rebindID, rejectedID} {
		if n := aogCount(t, e, "account.resubmit", id); n != 1 {
			t.Fatalf("admin edit wrote %d resubmit rows; want 1", n)
		}
	}
}

// 改绑撞号：409，原状态、原号不变，也不留重提审计。
func TestAOGRebindConflictKeepsState(t *testing.T) {
	e := w2cNewEnv(t)
	owner := e.user("aog-conflict@example.com", coreauth.RoleUser)
	admin := e.user("aog-conflict-admin@example.com", coreauth.RoleAdmin)
	school := e.school("aog-conflict")
	e.bind(admin.ID, school.ID)
	first := e.account(school.ID, owner.ID, "13800000040")
	second := e.account(school.ID, owner.ID, "13800000060")
	aogSetAccount(t, e, first, string(coreaccount.StatusActive), "", admin.ID)
	aogSetAccount(t, e, second, string(coreaccount.StatusActive), "", admin.ID)

	rec := e.do(http.MethodPatch, "/api/accounts/"+first,
		map[string]any{"phone": "13800000060", "current_password": w2cSeedPassword}, e.login(owner))
	w2cWantStatus(t, rec, http.StatusConflict)
	if code := e.errorCode(rec); code != "conflict" {
		t.Fatalf("code = %q, want conflict", code)
	}
	if got := e.scalarString("SELECT status::text || '|' || phone FROM accounts WHERE id = $1::uuid", first); got != "active|13800000040" {
		t.Fatalf("account after a conflicted rebind = %q, want active|13800000040", got)
	}
	if n := aogCount(t, e, "account.resubmit", first); n != 0 {
		t.Fatalf("conflicted rebind wrote %d resubmit rows; want 0", n)
	}
	if n := aogCount(t, e, "account.update", first); n != 0 {
		t.Fatalf("conflicted rebind wrote %d update rows; want 0", n)
	}
}
