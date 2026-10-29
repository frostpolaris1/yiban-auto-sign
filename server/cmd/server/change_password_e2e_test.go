package main

// 登录态自助改密的端到端测试。真库、真 HTTP、真 cookie。
//
// # 功能
// 覆盖改密端点的六条用户可见结果：旧口令错回 401 且不落库、
// 改密成功、其余会话被撤销、未登录被拒、缺 CSRF 被拒、审计行落库。
// 本文件**不**测性能，**不**复制仓储层用例。
//
// # 归属
// cmd/server 是唯一可同时引用 adapters、app、entry 的位置。
// 环境复用 auth_e2e_test.go 的 newAuthEnv。
//
// # 口径
// 缺 DSN 时由 auth_e2e_test.go 的 TestMain 打印横幅；用例自身跳过。
// 每个用例独占一个 schema，互不依赖。
// 口令全部取固定合成值，不来自真实对象。

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/cookiejar"
	"strings"
	"testing"
	"time"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// changePasswordPath 是登录态改密端点。
const changePasswordPath = "/api/auth/change-password"

// doAs 用给定客户端发一次 JSON 请求。
// 它让同一用例持有两个互不覆盖的 cookie 罐。
func (e *authEnv) doAs(client *http.Client, method, path string, body any, headers map[string]string) (*http.Response, []byte) {
	e.t.Helper()
	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			e.t.Fatalf("encode body: %v", err)
		}
		reader = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(e.ctx, method, e.baseURL+path, reader)
	if err != nil {
		e.t.Fatalf("new request: %v", err)
	}
	req.Header.Set("Content-Type", "application/json")
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := client.Do(req)
	if err != nil {
		e.t.Fatalf("%s %s: %v", method, path, err)
	}
	defer resp.Body.Close()
	payload, err := io.ReadAll(resp.Body)
	if err != nil {
		e.t.Fatalf("read body: %v", err)
	}
	return resp, payload
}

// newIsolatedClient 返回一条独立 cookie 罐的客户端。
// 第二个会话不能覆盖第一个会话的 cookie。
func newIsolatedClient(t *testing.T) *http.Client {
	t.Helper()
	jar, err := cookiejar.New(nil)
	if err != nil {
		t.Fatalf("cookie jar: %v", err)
	}
	return &http.Client{Jar: jar, Timeout: 10 * time.Second}
}

// loginOn 用给定客户端登录；失败即终止。
func (e *authEnv) loginOn(client *http.Client, email, password string) sessionUserBody {
	e.t.Helper()
	resp, payload := e.doAs(client, http.MethodPost, "/api/auth/login",
		map[string]string{"email": email, "password": password}, nil)
	if resp.StatusCode != http.StatusOK {
		e.t.Fatalf("login %s status = %d, want 200; body=%q", email, resp.StatusCode, payload)
	}
	var body sessionUserBody
	if err := json.Unmarshal(payload, &body); err != nil {
		e.t.Fatalf("decode login body: %v; body=%q", err, payload)
	}
	return body
}

// seedTempPasswordUser 造一个被要求改密的用户。
// 管理员建号发临时口令后的库内状态即 must_change_password = true。
func (e *authEnv) seedTempPasswordUser(email, password string) coreuser.Record {
	e.t.Helper()
	rec := e.createUser(email, password)
	if _, err := e.db.Exec(e.ctx,
		"UPDATE users SET must_change_password = true WHERE id = $1::uuid", rec.ID); err != nil {
		e.t.Fatalf("set must_change_password for %s: %v", email, err)
	}
	return rec
}

// changePassword 以给定 CSRF 令牌调用改密端点。
func (e *authEnv) changePassword(csrf, current, next string) (*http.Response, []byte) {
	e.t.Helper()
	return e.do(http.MethodPost, changePasswordPath,
		map[string]string{"current_password": current, "new_password": next},
		map[string]string{"X-CSRF-Token": csrf})
}

// liveSessions 数一个用户未撤销的会话。
func (e *authEnv) liveSessions(userID string) int64 {
	e.t.Helper()
	return e.scalarInt(
		"SELECT count(*) FROM sessions WHERE user_id = $1::uuid AND revoked_at IS NULL", userID)
}

// --- 旧口令错：401 且不落库 --------------------------------------------

// 模拟真实用户：登录后把旧口令输错，再提交改密。
// 断言用户可见结果：401 与登录失败同码同文案；库内口令与会话不变。
func TestChangePasswordRejectsWrongCurrentPassword(t *testing.T) {
	e := newAuthEnv(t)
	user := e.createUser("lena@example.com", "old-pass-123")
	_, body := e.login("lena@example.com", "old-pass-123")
	if body.CSRFToken == "" {
		t.Fatal("login returned no CSRF token")
	}
	beforeHash := e.scalarString("SELECT password_hash FROM users WHERE id = $1::uuid", user.ID)

	resp, payload := e.changePassword(body.CSRFToken, "wrong-pass-999", "new-pass-456")
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("wrong current password status = %d, want 401; body=%q", resp.StatusCode, payload)
	}
	code, message := errorCode(t, payload)
	if code != "unauthorized" {
		t.Fatalf("wrong current password code = %q, want unauthorized", code)
	}

	// 与登录失败的文案逐字相同：改密不得成为新的口令探测面。
	_, loginPayload := e.do(http.MethodPost, "/api/auth/login",
		map[string]string{"email": "lena@example.com", "password": "wrong-pass-999"}, nil)
	loginCode, loginMessage := errorCode(t, loginPayload)
	if code != loginCode || message != loginMessage {
		t.Fatalf("change-password 401 = %q/%q, login 401 = %q/%q; they must match",
			code, message, loginCode, loginMessage)
	}

	if got := e.scalarString("SELECT password_hash FROM users WHERE id = $1::uuid", user.ID); got != beforeHash {
		t.Fatal("a rejected change rewrote password_hash")
	}
	if got := e.scalarString("SELECT must_change_password::text FROM users WHERE id = $1::uuid", user.ID); got != "false" {
		t.Fatalf("must_change_password = %q after a rejected change, want false", got)
	}
	if got := e.scalarString(
		"SELECT coalesce(password_changed_at::text, '') FROM users WHERE id = $1::uuid", user.ID); got != "" {
		t.Fatalf("password_changed_at = %q after a rejected change, want NULL", got)
	}
	if got := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'auth.password_change'"); got != 0 {
		t.Fatalf("auth.password_change rows = %d after a rejected change, want 0", got)
	}
	if got := e.liveSessions(user.ID); got != 1 {
		t.Fatalf("live sessions = %d after a rejected change, want 1", got)
	}
}

// 旧口令错且新旧口令相同：必须先判 401，不得因为「新旧相同」回 409。
// 判据来自契约：409 只在旧口令校验通过后出现（审查 P3-1）。
func TestChangePasswordWrongCurrentBeatsReusedPassword(t *testing.T) {
	e := newAuthEnv(t)
	user := e.createUser("mia@example.com", "old-pass-123")
	_, body := e.login("mia@example.com", "old-pass-123")

	resp, payload := e.changePassword(body.CSRFToken, "wrong-pass-999", "wrong-pass-999")
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("wrong current + reused new status = %d, want 401; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarString("SELECT password_hash FROM users WHERE id = $1::uuid", user.ID); got == "" {
		t.Fatal("password_hash 不该被清空")
	}
	if got := e.liveSessions(user.ID); got != 1 {
		t.Fatalf("live sessions = %d, want 1", got)
	}
}

// --- 改密成功：新口令可用，旧口令不可用 --------------------------------

// 模拟真实用户：用临时口令登录，填旧口令与新口令，完成改密。
// 断言用户可见结果：204、当前会话继续可用、提示条消失、
// 新口令能登录而旧口令不能。
func TestChangePasswordSucceeds(t *testing.T) {
	e := newAuthEnv(t)
	user := e.seedTempPasswordUser("mike@example.com", "temp-pass-123")
	loginResp, body := e.login("mike@example.com", "temp-pass-123")
	if loginResp.StatusCode != http.StatusOK {
		t.Fatalf("login status = %d", loginResp.StatusCode)
	}
	if !body.MustChangePassword {
		t.Fatal("precondition: login did not report must_change_password")
	}
	beforeHash := e.scalarString("SELECT password_hash FROM users WHERE id = $1::uuid", user.ID)

	resp, payload := e.changePassword(body.CSRFToken, "temp-pass-123", "new-pass-456")
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("change password status = %d, want 204; body=%q", resp.StatusCode, payload)
	}
	if len(payload) != 0 {
		t.Fatalf("204 response carried a body: %q", payload)
	}

	// 当前会话保留：同一个 cookie 仍可读自己的资料，且提示条已消除。
	meResp, mePayload := e.do(http.MethodGet, "/api/auth/me", nil, nil)
	if meResp.StatusCode != http.StatusOK {
		t.Fatalf("me after change status = %d, want 200; body=%q", meResp.StatusCode, mePayload)
	}
	var me sessionUserBody
	if err := json.Unmarshal(mePayload, &me); err != nil {
		t.Fatalf("decode me: %v", err)
	}
	if me.MustChangePassword {
		t.Fatal("me still reports must_change_password after a successful change")
	}

	afterHash := e.scalarString("SELECT password_hash FROM users WHERE id = $1::uuid", user.ID)
	if !strings.HasPrefix(afterHash, "$argon2id$") {
		t.Fatalf("password_hash = %q, want an argon2id PHC string", afterHash)
	}
	if afterHash == beforeHash {
		t.Fatal("password_hash was not rewritten")
	}
	if got := e.scalarString("SELECT must_change_password::text FROM users WHERE id = $1::uuid", user.ID); got != "false" {
		t.Fatalf("must_change_password = %q, want false", got)
	}
	if got := e.scalarString(
		"SELECT coalesce(password_changed_at::text, '') FROM users WHERE id = $1::uuid", user.ID); got == "" {
		t.Fatal("password_changed_at was not written")
	}

	newLogin, newBody := e.login("mike@example.com", "new-pass-456")
	if newLogin.StatusCode != http.StatusOK {
		t.Fatalf("login with the new password = %d, want 200; body=%+v", newLogin.StatusCode, newBody)
	}
	oldLogin, oldPayload := e.do(http.MethodPost, "/api/auth/login",
		map[string]string{"email": "mike@example.com", "password": "temp-pass-123"}, nil)
	if oldLogin.StatusCode != http.StatusUnauthorized {
		t.Fatalf("login with the old password = %d, want 401; body=%q", oldLogin.StatusCode, oldPayload)
	}

	if got := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'auth.password_change'"); got != 1 {
		t.Fatalf("auth.password_change rows = %d, want 1", got)
	}
	label := e.scalarString("SELECT actor_label FROM audit_logs WHERE action = 'auth.password_change'")
	if label != "m***@example.com" {
		t.Fatalf("actor_label = %q, want the masked email", label)
	}
}

// --- 其余会话被撤销，当前会话保留 --------------------------------------

// 模拟真实用户：在两台设备登录，从第一台改密。
// 断言用户可见结果：第二台立即失效，第一台继续可用。
func TestChangePasswordRevokesOtherSessions(t *testing.T) {
	e := newAuthEnv(t)
	user := e.createUser("nina@example.com", "old-pass-123")

	firstResp, first := e.login("nina@example.com", "old-pass-123")
	if firstResp.StatusCode != http.StatusOK {
		t.Fatalf("first login status = %d", firstResp.StatusCode)
	}
	currentCookie := findCookie(firstResp, coreauth.InsecureCookieName)
	if currentCookie == nil {
		t.Fatal("first login returned no session cookie")
	}

	otherClient := newIsolatedClient(t)
	e.loginOn(otherClient, "nina@example.com", "old-pass-123")

	resp, payload := e.changePassword(first.CSRFToken, "old-pass-123", "new-pass-456")
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("change password status = %d, want 204; body=%q", resp.StatusCode, payload)
	}

	otherMe, otherPayload := e.doAs(otherClient, http.MethodGet, "/api/auth/me", nil, nil)
	if otherMe.StatusCode != http.StatusUnauthorized {
		t.Fatalf("second device me = %d, want 401; body=%q", otherMe.StatusCode, otherPayload)
	}
	currentMe, currentPayload := e.do(http.MethodGet, "/api/auth/me", nil, nil)
	if currentMe.StatusCode != http.StatusOK {
		t.Fatalf("current device me = %d, want 200; body=%q", currentMe.StatusCode, currentPayload)
	}

	if got := e.liveSessions(user.ID); got != 1 {
		t.Fatalf("live sessions = %d after a change, want 1", got)
	}
	// 存活的那条必须正是当前 cookie 对应的会话。
	kept := e.scalarInt(
		"SELECT count(*) FROM sessions WHERE user_id = $1::uuid AND revoked_at IS NULL AND token_hash = $2",
		user.ID, coreauth.HashToken(currentCookie.Value).Bytes())
	if kept != 1 {
		t.Fatal("the surviving session is not the calling one")
	}
}

// --- 未登录：401 --------------------------------------------------------

// 模拟真实用户：未登录就调用改密端点。
// 断言用户可见结果：401，且不泄露端点是否存在。
func TestChangePasswordRequiresSession(t *testing.T) {
	e := newAuthEnv(t)
	resp, payload := e.do(http.MethodPost, changePasswordPath,
		map[string]string{"current_password": "old-pass-123", "new_password": "new-pass-456"}, nil)
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("session-less change status = %d, want 401; body=%q", resp.StatusCode, payload)
	}
	if code, _ := errorCode(t, payload); code != "unauthorized" {
		t.Fatalf("session-less change code = %q, want unauthorized", code)
	}
}

// --- 缺 CSRF：403 -------------------------------------------------------

// 模拟真实用户：带会话但不带 X-CSRF-Token 提交改密。
// 断言用户可见结果：403，且口令与审计都不变。
func TestChangePasswordRequiresCSRF(t *testing.T) {
	e := newAuthEnv(t)
	user := e.createUser("olga@example.com", "old-pass-123")
	loginResp, _ := e.login("olga@example.com", "old-pass-123")
	if loginResp.StatusCode != http.StatusOK {
		t.Fatalf("login status = %d, want 200", loginResp.StatusCode)
	}
	beforeHash := e.scalarString("SELECT password_hash FROM users WHERE id = $1::uuid", user.ID)

	resp, payload := e.do(http.MethodPost, changePasswordPath,
		map[string]string{"current_password": "old-pass-123", "new_password": "new-pass-456"}, nil)
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("change without CSRF = %d, want 403; body=%q", resp.StatusCode, payload)
	}
	if code, _ := errorCode(t, payload); code != "forbidden" {
		t.Fatalf("change without CSRF code = %q, want forbidden", code)
	}
	if got := e.scalarString("SELECT password_hash FROM users WHERE id = $1::uuid", user.ID); got != beforeHash {
		t.Fatal("a CSRF-rejected change rewrote password_hash")
	}
	if got := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'auth.password_change'"); got != 0 {
		t.Fatalf("auth.password_change rows = %d after a CSRF rejection, want 0", got)
	}
}

// --- 新口令等于旧口令：409 ----------------------------------------------

// 模拟真实用户：把新口令填成与旧口令相同。
// 断言用户可见结果：409 conflict，且口令不变。
func TestChangePasswordRejectsReusedPassword(t *testing.T) {
	e := newAuthEnv(t)
	user := e.createUser("pete@example.com", "old-pass-123")
	_, body := e.login("pete@example.com", "old-pass-123")
	beforeHash := e.scalarString("SELECT password_hash FROM users WHERE id = $1::uuid", user.ID)

	resp, payload := e.changePassword(body.CSRFToken, "old-pass-123", "old-pass-123")
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("reused password status = %d, want 409; body=%q", resp.StatusCode, payload)
	}
	if code, _ := errorCode(t, payload); code != "conflict" {
		t.Fatalf("reused password code = %q, want conflict", code)
	}
	if got := e.scalarString("SELECT password_hash FROM users WHERE id = $1::uuid", user.ID); got != beforeHash {
		t.Fatal("a rejected reuse rewrote password_hash")
	}
	if got := e.liveSessions(user.ID); got != 1 {
		t.Fatalf("live sessions = %d after a rejected reuse, want 1", got)
	}
	if got := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'auth.password_change'"); got != 0 {
		t.Fatalf("auth.password_change rows = %d after a rejected reuse, want 0", got)
	}
}

// --- 空新口令：400 ------------------------------------------------------

// 模拟真实用户：新口令留空提交。
// 断言用户可见结果：400 validation_failed，且口令不变。
func TestChangePasswordRejectsEmptyNewPassword(t *testing.T) {
	e := newAuthEnv(t)
	user := e.createUser("quinn@example.com", "old-pass-123")
	_, body := e.login("quinn@example.com", "old-pass-123")
	beforeHash := e.scalarString("SELECT password_hash FROM users WHERE id = $1::uuid", user.ID)

	resp, payload := e.changePassword(body.CSRFToken, "old-pass-123", "")
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("empty new password status = %d, want 400; body=%q", resp.StatusCode, payload)
	}
	if code, _ := errorCode(t, payload); code != "validation_failed" {
		t.Fatalf("empty new password code = %q, want validation_failed", code)
	}
	if got := e.scalarString("SELECT password_hash FROM users WHERE id = $1::uuid", user.ID); got != beforeHash {
		t.Fatal("a rejected empty password rewrote password_hash")
	}
}
