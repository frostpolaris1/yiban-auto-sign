package main

// phc：危险操作二次口令与登录口令策略的端到端测试。真库、真 HTTP、真 cookie。
//
// # 功能
// 每个危险端点钉住 missing→400 / wrong→403 / right→原行为 与「失败零写入零审计」；
// 账号面钉住 name-only 不要口令、凭据面要口令；策略面钉住 user 10/2 与
// master/admin 12/3 的正负例、易班账号口令不受限、节流 5 次后 429 且成功清零。
// 本文件**不**测性能，**不**复制用例层单测。
//
// # 归属
// cmd/server 是唯一可同时引用 adapters、app、entry 的位置。
// 环境复用 auth_e2e_test.go 的 newAuthEnv。
//
// # 口径
// 缺 DSN 时整包跳过并打印横幅。每个用例独占一个 schema；
// 节流计数按 user id 分桶，每个用例的用户都是新 id，互不串扰。
// 口令全部取固定合成值，不来自真实对象。

import (
	"encoding/json"
	"net/http"
	"strconv"
	"strings"
	"testing"
	"time"

	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

// phcAuditCount 数某 target 的全部审计行（任何动作）。
func phcAuditCount(t *testing.T, e *authEnv, targetID string) int64 {
	t.Helper()
	return e.scalarInt("SELECT count(*) FROM audit_logs WHERE target_id = $1", targetID)
}

// phcLoginMaster 建一名 master 并登录，返回会话与目标环境里的用户行。
func phcLoginMaster(t *testing.T, e *authEnv, email, password string) sessionUserBody {
	t.Helper()
	w2dSeedUser(t, e, email, password, coreauth.RoleMaster)
	// 登录在环境默认客户端上：同用例的 e.do 走同一 cookie 罐。
	return u1LoginOn(e, e.client, email, password)
}

// --- PATCH /api/users/{id}：missing/wrong/right 与零写入 ------------------

func TestPHCReauthUserPatchGate(t *testing.T) {
	e := newAuthEnv(t)
	session := phcLoginMaster(t, e, "phc-patch-boss@example.com", "boss-pass")
	target := w2dSeedUser(t, e, "phc-patch-target@example.com", "target-pass", coreauth.RoleUser)
	path := "/api/users/" + target.ID
	csrf := map[string]string{"X-CSRF-Token": session.CSRFToken}

	// 缺口令：400，角色未变，零审计。
	resp, payload := e.do(http.MethodPatch, path,
		map[string]any{"role": "admin"}, csrf)
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("missing password = %d, want 400; body=%q", resp.StatusCode, payload)
	}
	if code, _ := errorCode(t, payload); code != "validation_failed" {
		t.Fatalf("missing password code = %q, want validation_failed", code)
	}
	if got := e.scalarString("SELECT role::text FROM users WHERE id = $1", target.ID); got != "user" {
		t.Fatalf("role after missing password = %q, want user", got)
	}
	if n := phcAuditCount(t, e, target.ID); n != 0 {
		t.Fatalf("audit rows after missing password = %d, want 0", n)
	}

	// 错误口令：403，角色未变，零审计。
	resp, payload = e.do(http.MethodPatch, path,
		map[string]any{"role": "admin", "current_password": "wrong-pass-42"}, csrf)
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("wrong password = %d, want 403; body=%q", resp.StatusCode, payload)
	}
	if code, _ := errorCode(t, payload); code != "forbidden" {
		t.Fatalf("wrong password code = %q, want forbidden", code)
	}
	if got := e.scalarString("SELECT role::text FROM users WHERE id = $1", target.ID); got != "user" {
		t.Fatalf("role after wrong password = %q, want user", got)
	}
	if n := phcAuditCount(t, e, target.ID); n != 0 {
		t.Fatalf("audit rows after wrong password = %d, want 0", n)
	}

	// 正确口令：200 且角色变更落库、审计一行。
	resp, payload = e.do(http.MethodPatch, path,
		map[string]any{"role": "admin", "current_password": "boss-pass"}, csrf)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("right password = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarString("SELECT role::text FROM users WHERE id = $1", target.ID); got != "admin" {
		t.Fatalf("role after right password = %q, want admin", got)
	}
	if n := e.scalarInt("SELECT count(*) FROM audit_logs WHERE target_id = $1 AND action = 'user.role_change'", target.ID); n != 1 {
		t.Fatalf("user.role_change rows = %d, want 1", n)
	}
}

// --- DELETE /api/users/{id} ------------------------------------------------

func TestPHCReauthUserDeleteGate(t *testing.T) {
	e := newAuthEnv(t)
	session := phcLoginMaster(t, e, "phc-del-boss@example.com", "boss-pass")
	target := w2dSeedUser(t, e, "phc-del-target@example.com", "target-pass", coreauth.RoleUser)
	path := "/api/users/" + target.ID
	csrf := map[string]string{"X-CSRF-Token": session.CSRFToken}

	resp, payload := e.do(http.MethodDelete, path, nil, csrf)
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("missing password = %d, want 400; body=%q", resp.StatusCode, payload)
	}
	resp, payload = e.do(http.MethodDelete, path,
		map[string]any{"current_password": "wrong-pass-42"}, csrf)
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("wrong password = %d, want 403; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarString("SELECT coalesce(deleted_at::text, '') FROM users WHERE id = $1", target.ID); got != "" {
		t.Fatal("a rejected delete soft-deleted the user")
	}
	if n := phcAuditCount(t, e, target.ID); n != 0 {
		t.Fatalf("audit rows after rejected deletes = %d, want 0", n)
	}

	resp, payload = e.do(http.MethodDelete, path,
		map[string]any{"current_password": "boss-pass"}, csrf)
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("right password = %d, want 204; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarString("SELECT coalesce(deleted_at::text, '') FROM users WHERE id = $1", target.ID); got == "" {
		t.Fatal("the right password did not delete the user")
	}
	if n := e.scalarInt("SELECT count(*) FROM audit_logs WHERE target_id = $1 AND action = 'user.delete'", target.ID); n != 1 {
		t.Fatalf("user.delete rows = %d, want 1", n)
	}
}

// --- POST /api/users/{id}/password-reset -----------------------------------

func TestPHCReauthUserPasswordResetGate(t *testing.T) {
	e := newAuthEnv(t)
	session := phcLoginMaster(t, e, "phc-reset-boss@example.com", "boss-pass")
	target := w2dSeedUser(t, e, "phc-reset-target@example.com", "target-pass", coreauth.RoleUser)
	path := "/api/users/" + target.ID + "/password-reset"
	csrf := map[string]string{"X-CSRF-Token": session.CSRFToken}
	beforeHash := e.scalarString("SELECT password_hash FROM users WHERE id = $1", target.ID)

	resp, payload := e.do(http.MethodPost, path, nil, csrf)
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("missing password = %d, want 400; body=%q", resp.StatusCode, payload)
	}
	resp, payload = e.do(http.MethodPost, path,
		map[string]any{"current_password": "wrong-pass-42"}, csrf)
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("wrong password = %d, want 403; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarString("SELECT password_hash FROM users WHERE id = $1", target.ID); got != beforeHash {
		t.Fatal("a rejected reset rewrote password_hash")
	}
	if n := phcAuditCount(t, e, target.ID); n != 0 {
		t.Fatalf("audit rows after rejected resets = %d, want 0", n)
	}

	resp, payload = e.do(http.MethodPost, path,
		map[string]any{"current_password": "boss-pass"}, csrf)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("right password = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	var temp struct {
		TempPassword string `json:"temp_password"`
	}
	if err := json.Unmarshal(payload, &temp); err != nil {
		t.Fatalf("decode temp: %v; body=%q", err, payload)
	}
	if temp.TempPassword == "" {
		t.Fatal("temp_password is empty")
	}
	// 临时口令满足目标角色的档位策略。
	if reason := coreauth.PasswordPolicyErrorForRole(temp.TempPassword, coreauth.RoleUser); reason != "" {
		t.Fatalf("temp_password violates the target policy: %q", reason)
	}
	// 临时口令能登录，且不进审计。
	if loginResp, _ := e.login("phc-reset-target@example.com", temp.TempPassword); loginResp.StatusCode != http.StatusOK {
		t.Fatalf("temp password login = %d, want 200", loginResp.StatusCode)
	}
	if n := e.scalarInt("SELECT count(*) FROM audit_logs WHERE target_id = $1 AND action = 'user.password_reset'", target.ID); n != 1 {
		t.Fatalf("user.password_reset rows = %d, want 1", n)
	}
	detail := e.scalarString("SELECT coalesce(string_agg(detail, ''), '') FROM audit_logs WHERE target_id = $1 AND action = 'user.password_reset'", target.ID)
	if strings.Contains(detail, temp.TempPassword) {
		t.Fatalf("user.password_reset detail carries the temp password: %q", detail)
	}
}

// --- PATCH /api/accounts/{id}：name 不要口令，凭据面要口令 -----------------

func TestPHCReauthAccountPatchGate(t *testing.T) {
	e := newAuthEnv(t)
	owner := w2dSeedUser(t, e, "phc-acct@example.com", "owner-pass-42", coreauth.RoleUser)
	account := ux8SeedAccount(t, e, owner.ID, "13500135011")
	path := "/api/accounts/" + account.ID

	client := u1FreshClient(t)
	session := u1LoginOn(e, client, "phc-acct@example.com", "owner-pass-42")
	csrf := map[string]string{"X-CSRF-Token": session.CSRFToken}

	// 只改 name：不要二次口令，200。
	resp, payload := e.doAs(client, http.MethodPatch, path, map[string]any{"name": "备注名"}, csrf)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("name-only edit = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	// name-only 成功本身写一条审计：后续零写断言以它为基线。
	baseline := phcAuditCount(t, e, account.ID)

	// phone 缺口令：400，手机号未变。
	resp, payload = e.doAs(client, http.MethodPatch, path, map[string]any{"phone": "13500135012"}, csrf)
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("phone without password = %d, want 400; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarString("SELECT phone FROM accounts WHERE id = $1", account.ID); got != "13500135011" {
		t.Fatalf("phone after missing password = %q, want unchanged", got)
	}

	// phone 错误口令：403，手机号未变，零审计。
	resp, payload = e.doAs(client, http.MethodPatch, path,
		map[string]any{"phone": "13500135012", "current_password": "wrong-pass-42"}, csrf)
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("phone with wrong password = %d, want 403; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarString("SELECT phone FROM accounts WHERE id = $1", account.ID); got != "13500135011" {
		t.Fatalf("phone after wrong password = %q, want unchanged", got)
	}
	if n := phcAuditCount(t, e, account.ID); n != baseline {
		t.Fatalf("audit rows after rejected phone edits = %d, want %d", n, baseline)
	}

	// phone 正确口令：200，换绑回 pending。
	resp, payload = e.doAs(client, http.MethodPatch, path,
		map[string]any{"phone": "13500135012", "current_password": "owner-pass-42"}, csrf)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("phone with right password = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarString("SELECT phone FROM accounts WHERE id = $1", account.ID); got != "13500135012" {
		t.Fatalf("phone after right password = %q, want the new number", got)
	}
	if got := e.scalarString("SELECT status::text FROM accounts WHERE id = $1", account.ID); got != string(coreaccount.StatusPending) {
		t.Fatalf("status after rebind = %q, want pending", got)
	}

	// 易班账号口令不受本站策略：弱值也保存（正确的二次口令）。
	resp, payload = e.doAs(client, http.MethodPatch, path,
		map[string]any{"password": "abc", "current_password": "owner-pass-42"}, csrf)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("weak platform password = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	if n := e.scalarInt("SELECT count(*) FROM audit_logs WHERE target_id = $1 AND action = 'account.update'", account.ID); n < 1 {
		t.Fatalf("account.update rows = %d, want at least 1", n)
	}
}

// --- DELETE /api/accounts/{id} ---------------------------------------------

func TestPHCReauthAccountDeleteGate(t *testing.T) {
	e := newAuthEnv(t)
	owner := w2dSeedUser(t, e, "phc-acct-del@example.com", "owner-pass-42", coreauth.RoleUser)
	account := ux8SeedAccount(t, e, owner.ID, "13500135013")
	path := "/api/accounts/" + account.ID

	client := u1FreshClient(t)
	session := u1LoginOn(e, client, "phc-acct-del@example.com", "owner-pass-42")
	csrf := map[string]string{"X-CSRF-Token": session.CSRFToken}

	resp, payload := e.doAs(client, http.MethodDelete, path, nil, csrf)
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("missing password = %d, want 400; body=%q", resp.StatusCode, payload)
	}
	resp, payload = e.doAs(client, http.MethodDelete, path,
		map[string]any{"current_password": "wrong-pass-42"}, csrf)
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("wrong password = %d, want 403; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarString("SELECT coalesce(deleted_at::text, '') FROM accounts WHERE id = $1", account.ID); got != "" {
		t.Fatal("a rejected delete soft-deleted the account")
	}
	if n := phcAuditCount(t, e, account.ID); n != 0 {
		t.Fatalf("audit rows after rejected deletes = %d, want 0", n)
	}

	resp, payload = e.doAs(client, http.MethodDelete, path,
		map[string]any{"current_password": "owner-pass-42"}, csrf)
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("right password = %d, want 204; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarString("SELECT coalesce(deleted_at::text, '') FROM accounts WHERE id = $1", account.ID); got == "" {
		t.Fatal("the right password did not delete the account")
	}
	if n := e.scalarInt("SELECT count(*) FROM audit_logs WHERE target_id = $1 AND action = 'account.delete'", account.ID); n != 1 {
		t.Fatalf("account.delete rows = %d, want 1", n)
	}
}

// --- POST /api/me/accounts/{id}/delete -------------------------------------

func TestPHCReauthMyAccountDeleteGate(t *testing.T) {
	e := newAuthEnv(t)
	owner := w2dSeedUser(t, e, "phc-self-del@example.com", "owner-pass-42", coreauth.RoleUser)
	account := ux8SeedAccount(t, e, owner.ID, "13500135014")
	path := "/api/me/accounts/" + account.ID + "/delete"

	client := u1FreshClient(t)
	session := u1LoginOn(e, client, "phc-self-del@example.com", "owner-pass-42")
	csrf := map[string]string{"X-CSRF-Token": session.CSRFToken}

	resp, payload := e.doAs(client, http.MethodPost, path, nil, csrf)
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("missing password = %d, want 400; body=%q", resp.StatusCode, payload)
	}
	resp, payload = e.doAs(client, http.MethodPost, path,
		map[string]any{"current_password": "wrong-pass-42"}, csrf)
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("wrong password = %d, want 403; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarString("SELECT coalesce(deleted_at::text, '') FROM accounts WHERE id = $1", account.ID); got != "" {
		t.Fatal("a rejected self delete soft-deleted the account")
	}
	if n := phcAuditCount(t, e, account.ID); n != 0 {
		t.Fatalf("audit rows after rejected self deletes = %d, want 0", n)
	}

	resp, payload = e.doAs(client, http.MethodPost, path,
		map[string]any{"current_password": "owner-pass-42"}, csrf)
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("right password = %d, want 204; body=%q", resp.StatusCode, payload)
	}
	if n := e.scalarInt("SELECT count(*) FROM audit_logs WHERE target_id = $1 AND action = 'account.self_delete'", account.ID); n != 1 {
		t.Fatalf("account.self_delete rows = %d, want 1", n)
	}
}

// --- POST /api/accounts/{id}/device-refresh --------------------------------

func TestPHCReauthDeviceRefreshGate(t *testing.T) {
	e := newAuthEnv(t)
	owner := w2dSeedUser(t, e, "phc-device@example.com", "owner-pass-42", coreauth.RoleUser)
	account := ux8SeedAccount(t, e, owner.ID, "13500135015")
	path := "/api/accounts/" + account.ID + "/device-refresh"

	client := u1FreshClient(t)
	session := u1LoginOn(e, client, "phc-device@example.com", "owner-pass-42")
	csrf := map[string]string{"X-CSRF-Token": session.CSRFToken}

	resp, payload := e.doAs(client, http.MethodPost, path, nil, csrf)
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("missing password = %d, want 400; body=%q", resp.StatusCode, payload)
	}
	resp, payload = e.doAs(client, http.MethodPost, path,
		map[string]any{"current_password": "wrong-pass-42"}, csrf)
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("wrong password = %d, want 403; body=%q", resp.StatusCode, payload)
	}
	if n := phcAuditCount(t, e, account.ID); n != 0 {
		t.Fatalf("audit rows after rejected refreshes = %d, want 0", n)
	}
	// 正确口令：门已过，进入用例（本环境无平台，回 502 且零写）。
	// 断言「不是 400/403/429」即门放行；写路径行为由 u2Env 套件钉住。
	resp, payload = e.doAs(client, http.MethodPost, path,
		map[string]any{"current_password": "owner-pass-42"}, csrf)
	if resp.StatusCode == http.StatusBadRequest || resp.StatusCode == http.StatusForbidden ||
		resp.StatusCode == http.StatusTooManyRequests {
		t.Fatalf("right password hit the gate: %d; body=%q", resp.StatusCode, payload)
	}
	if n := phcAuditCount(t, e, account.ID); n != 0 {
		t.Fatalf("audit rows after the gate passed = %d, want 0 (no platform here)", n)
	}
}

// --- 节流：5 次失败后 429，成功清零 ----------------------------------------

func TestPHCReauthThrottle(t *testing.T) {
	e := newAuthEnv(t)
	session := phcLoginMaster(t, e, "phc-throttle-boss@example.com", "boss-pass")
	target := w2dSeedUser(t, e, "phc-throttle-target@example.com", "target-pass", coreauth.RoleUser)
	path := "/api/users/" + target.ID
	csrf := map[string]string{"X-CSRF-Token": session.CSRFToken}

	for i := 0; i < 5; i++ {
		resp, payload := e.do(http.MethodPatch, path,
			map[string]any{"role": "admin", "current_password": "wrong-pass-42"}, csrf)
		if resp.StatusCode != http.StatusForbidden {
			t.Fatalf("wrong attempt %d = %d, want 403; body=%q", i+1, resp.StatusCode, payload)
		}
	}
	// 第 6 次：即使口令正确也 429。
	resp, payload := e.do(http.MethodPatch, path,
		map[string]any{"role": "admin", "current_password": "boss-pass"}, csrf)
	if resp.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("sixth attempt = %d, want 429; body=%q", resp.StatusCode, payload)
	}
	if code, _ := errorCode(t, payload); code != "rate_limited" {
		t.Fatalf("sixth attempt code = %q, want rate_limited", code)
	}
	// 429 必须带 Retry-After：秒数存在且 >0。
	retryAfter := resp.Header.Get("Retry-After")
	if retryAfter == "" {
		t.Fatal("429 is missing the Retry-After header")
	}
	if seconds, err := strconv.Atoi(retryAfter); err != nil || seconds <= 0 {
		t.Fatalf("Retry-After = %q, want a positive integer", retryAfter)
	}
	if got := e.scalarString("SELECT role::text FROM users WHERE id = $1", target.ID); got != "user" {
		t.Fatal("the throttled window still changed the role")
	}

	// 另一个用户不受影响：第二名 master 正常操作。
	w2dSeedUser(t, e, "phc-throttle-boss2@example.com", "boss2-pass-42", coreauth.RoleMaster)
	client2 := u1FreshClient(t)
	session2 := u1LoginOn(e, client2, "phc-throttle-boss2@example.com", "boss2-pass-42")
	resp2, payload2 := e.doAs(client2, http.MethodPatch, path,
		map[string]any{"role": "admin", "current_password": "boss2-pass-42"},
		map[string]string{"X-CSRF-Token": session2.CSRFToken})
	if resp2.StatusCode != http.StatusOK {
		t.Fatalf("another user was throttled: %d; body=%q", resp2.StatusCode, payload2)
	}
}

// 成功清零：2 次失败 + 1 次成功后，重新数满 5 次才 429。
func TestPHCReauthThrottleClearsOnSuccess(t *testing.T) {
	e := newAuthEnv(t)
	session := phcLoginMaster(t, e, "phc-clear-boss@example.com", "boss-pass")
	target := w2dSeedUser(t, e, "phc-clear-target@example.com", "target-pass", coreauth.RoleUser)
	path := "/api/users/" + target.ID
	csrf := map[string]string{"X-CSRF-Token": session.CSRFToken}

	for i := 0; i < 2; i++ {
		resp, _ := e.do(http.MethodPatch, path,
			map[string]any{"role": "admin", "current_password": "wrong-pass-42"}, csrf)
		if resp.StatusCode != http.StatusForbidden {
			t.Fatalf("wrong attempt %d = %d, want 403", i+1, resp.StatusCode)
		}
	}
	// 成功一次：计数清零。
	resp, _ := e.do(http.MethodPatch, path,
		map[string]any{"status": "disabled", "current_password": "boss-pass"}, csrf)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("success after failures = %d, want 200", resp.StatusCode)
	}
	// 再来 5 次失败才 429；第 5 次仍是 403。
	for i := 0; i < 5; i++ {
		resp, _ := e.do(http.MethodPatch, path,
			map[string]any{"role": "admin", "current_password": "wrong-pass-42"}, csrf)
		if resp.StatusCode != http.StatusForbidden {
			t.Fatalf("failure %d after reset = %d, want 403 (counter was cleared)", i+1, resp.StatusCode)
		}
	}
	resp, payload := e.do(http.MethodPatch, path,
		map[string]any{"role": "admin", "current_password": "boss-pass"}, csrf)
	if resp.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("sixth failure after reset = %d, want 429; body=%q", resp.StatusCode, payload)
	}
}

// --- 口令策略：change-password 按 role 分档 --------------------------------

func TestPHCPasswordPolicyChangePassword(t *testing.T) {
	e := newAuthEnv(t)
	// user 档：9 字符 400；10 字符一类 400；10 字符两类 204。
	w2dSeedUser(t, e, "phc-pol-user@example.com", "user-pass-42", coreauth.RoleUser)
	client := u1FreshClient(t)
	session := u1LoginOn(e, client, "phc-pol-user@example.com", "user-pass-42")

	cases := []struct {
		name string
		next string
		want int
	}{
		{"nine chars", "Ab1!ab1!a", http.StatusBadRequest},
		{"ten chars one class", "abcdefghij", http.StatusBadRequest},
		{"eleven chars two classes", "abcdefghij1", http.StatusNoContent},
	}
	for _, tc := range cases {
		resp, payload := e.doAs(client, http.MethodPost, "/api/auth/change-password",
			map[string]string{"current_password": "user-pass-42", "new_password": tc.next},
			map[string]string{"X-CSRF-Token": session.CSRFToken})
		if resp.StatusCode != tc.want {
			t.Fatalf("%s = %d, want %d; body=%q", tc.name, resp.StatusCode, tc.want, payload)
		}
		if tc.want == http.StatusBadRequest {
			if code, _ := errorCode(t, payload); code != "validation_failed" {
				t.Fatalf("%s code = %q, want validation_failed", tc.name, code)
			}
		}
	}
	// 成功后新口令可登录。
	if resp, _ := e.login("phc-pol-user@example.com", "abcdefghij1"); resp.StatusCode != http.StatusOK {
		t.Fatalf("login with the accepted new password = %d, want 200", resp.StatusCode)
	}

	// 特权档：master 11 字符三类也拒；12 字符三类通过。
	if _, err := e.db.Exec(e.ctx,
		"UPDATE users SET role = 'master' WHERE email = 'phc-pol-user@example.com'"); err != nil {
		t.Fatalf("promote to master: %v", err)
	}
	// 角色改库后会话按行解析（入口每请求重读），当前 cookie 仍是同一用户。
	resp, payload := e.doAs(client, http.MethodPost, "/api/auth/change-password",
		map[string]string{"current_password": "abcdefghij1", "new_password": "Ab1!ab1!ab1"},
		map[string]string{"X-CSRF-Token": session.CSRFToken})
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("master 11 chars 3 classes = %d, want 400; body=%q", resp.StatusCode, payload)
	}
	if code, _ := errorCode(t, payload); code != "validation_failed" {
		t.Fatalf("master 11 chars code = %q, want validation_failed", code)
	}
	resp, payload = e.doAs(client, http.MethodPost, "/api/auth/change-password",
		map[string]string{"current_password": "abcdefghij1", "new_password": "Ab1!ab1!ab1!"},
		map[string]string{"X-CSRF-Token": session.CSRFToken})
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("master 12 chars 3 classes = %d, want 204; body=%q", resp.StatusCode, payload)
	}
}

// --- 口令策略：reset-password 按令牌对应用户角色分档 -----------------------

func TestPHCPasswordPolicyResetByToken(t *testing.T) {
	e := newAuthEnv(t)
	// 特权档目标：11 字符三类拒，12 字符三类过。
	target := w2dSeedUser(t, e, "phc-pol-reset@example.com", "reset-pass-42", coreauth.RoleMaster)
	token := "phc-policy-reset-token-0123456789"
	if err := e.seedResetToken(target.ID, token); err != nil {
		t.Fatalf("seed reset token: %v", err)
	}
	resp, payload := e.do(http.MethodPost, "/api/auth/reset-password",
		map[string]string{"token": token, "new_password": "Ab1!ab1!ab1"}, nil)
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("master 11 chars via token = %d, want 400; body=%q", resp.StatusCode, payload)
	}
	if code, _ := errorCode(t, payload); code != "validation_failed" {
		t.Fatalf("master 11 chars via token code = %q, want validation_failed", code)
	}
	// 弱令牌不消费：同一令牌换强口令仍可用。
	resp, payload = e.do(http.MethodPost, "/api/auth/reset-password",
		map[string]string{"token": token, "new_password": "Ab1!ab1!ab1!"}, nil)
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("master 12 chars via token = %d, want 204; body=%q", resp.StatusCode, payload)
	}
	if loginResp, _ := e.login("phc-pol-reset@example.com", "Ab1!ab1!ab1!"); loginResp.StatusCode != http.StatusOK {
		t.Fatalf("login after policy-passing reset = %d, want 200", loginResp.StatusCode)
	}
}

// --- 口令策略：POST /api/users 按目标角色分档 ------------------------------

func TestPHCPasswordPolicyUserCreate(t *testing.T) {
	e := newAuthEnv(t)
	session := phcLoginMaster(t, e, "phc-pol-create-boss@example.com", "boss-pass")
	csrf := map[string]string{"X-CSRF-Token": session.CSRFToken}
	path := "/api/users"

	cases := []struct {
		name     string
		email    string
		password string
		role     string
		want     int
	}{
		{"user 9 chars", "phc-pol-c1@example.com", "Ab1!ab1!a", "user", http.StatusBadRequest},
		{"user 10 chars one class", "phc-pol-c2@example.com", "abcdefghij", "user", http.StatusBadRequest},
		{"user 10 chars two classes", "phc-pol-c3@example.com", "abcdefghij1", "user", http.StatusCreated},
		{"admin 11 chars three classes", "phc-pol-c4@example.com", "Ab1!ab1!ab1", "admin", http.StatusBadRequest},
		{"admin 12 chars three classes", "phc-pol-c5@example.com", "Ab1!ab1!ab1!", "admin", http.StatusCreated},
		{"admin 12 chars two classes", "phc-pol-c6@example.com", "abcdefghij12", "admin", http.StatusBadRequest},
	}
	for _, tc := range cases {
		resp, payload := e.do(http.MethodPost, path,
			map[string]any{"email": tc.email, "password": tc.password, "role": tc.role}, csrf)
		if resp.StatusCode != tc.want {
			t.Fatalf("%s = %d, want %d; body=%q", tc.name, resp.StatusCode, tc.want, payload)
		}
	}
}

// seedResetToken 直接写一条未消费的重置令牌。
func (e *authEnv) seedResetToken(userID, token string) error {
	e.t.Helper()
	now := time.Now().UTC()
	_, err := e.resets.Create(e.ctx, coreauth.NewPasswordReset{
		UserID:    userID,
		TokenHash: coreauth.HashToken(token),
		Now:       now,
		ExpiresAt: now.Add(30 * time.Minute),
	})
	return err
}
