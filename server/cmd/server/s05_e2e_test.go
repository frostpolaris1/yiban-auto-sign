package main

// S05 e2e：公开注册。
//
// # 功能
// 钉住五条用户可见的性质：
//   1. 默认关闭注册：公开探测回 false，提交回 403，且不建用户、不留注册审计。
//   2. master 开关：公开探测随开关变化，非 master 写回 403，写动作留审计。
//   3. 注册即登录：201 建 user/active 账号，回会话 cookie，me 可用，口令是 argon2id。
//   4. 校验：弱口令、未勾选协议、陈旧协议版本、非法邮箱、重复邮箱都被拒，且不建用户。
//   5. 限流与跨站：同 IP 第二次注册 429；跨站浏览器提交 403。
//
// # 归属
// cmd/server 用完整 assembly 与真实 PostgreSQL；端口与契约同形。
//
// # 口径
// 缺 DSN 时 newAuthEnv 显式跳过，跳过不是绿。
// 邮箱一律 example.com；手机号不出现。每个用例独占 schema。

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/cookiejar"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// s05RegisterBody 是合法的注册请求体。
func s05RegisterBody(email, password string) map[string]any {
	return map[string]any{
		"email":           email,
		"password":        password,
		"agree_terms":     true,
		"agree_privacy":   true,
		"terms_version":   1,
		"privacy_version": 1,
	}
}

// s05Password 满足最小策略：10 位以上，含大小写、数字与符号。
const s05Password = "Correct-horse-9!"

type s05Registration struct {
	Enabled        bool `json:"enabled"`
	TermsVersion   int  `json:"terms_version"`
	PrivacyVersion int  `json:"privacy_version"`
}

type s05Session struct {
	ID                 string `json:"id"`
	Email              string `json:"email"`
	Role               string `json:"role"`
	Status             string `json:"status"`
	MustChangePassword bool   `json:"must_change_password"`
	CSRFToken          string `json:"csrf_token"`
}

// s05Client 造一个独立 cookie 客户端，代表另一个浏览器。
func s05Client(t *testing.T) *http.Client {
	t.Helper()
	jar, err := cookiejar.New(nil)
	if err != nil {
		t.Fatalf("cookie jar: %v", err)
	}
	return &http.Client{Jar: jar, Timeout: 10 * time.Second}
}

// s05Do 用指定客户端发一次 JSON 请求。每次调用都关掉连接复用，
// 避免测试之间把服务端连接状态当成产品行为。
func s05Do(t *testing.T, e *authEnv, client *http.Client, method, path string, body any, headers map[string]string) (*http.Response, []byte) {
	t.Helper()
	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			t.Fatalf("marshal request: %v", err)
		}
		reader = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(e.ctx, method, e.baseURL+path, reader)
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	for name, value := range headers {
		req.Header.Set(name, value)
	}
	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("%s %s: %v", method, path, err)
	}
	defer resp.Body.Close()
	payload, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("read response: %v", err)
	}
	return resp, payload
}

// s05Status 读公开注册状态。
func s05Status(t *testing.T, e *authEnv) s05Registration {
	t.Helper()
	resp, payload := s05Do(t, e, s05Client(t), http.MethodGet, "/api/auth/registration", nil, nil)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("GET registration = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	var status s05Registration
	if err := json.Unmarshal(payload, &status); err != nil {
		t.Fatalf("decode registration status: %v; body=%q", err, payload)
	}
	return status
}

// s05Master 造一个 master 用户并登录默认客户端。返回 CSRF 令牌。
func s05Master(t *testing.T, e *authEnv) string {
	t.Helper()
	hash, err := coreauth.HashPassword("master-password-9!")
	if err != nil {
		t.Fatalf("hash master password: %v", err)
	}
	var rec coreuser.Record
	err = e.db.InTx(e.ctx, func(ctx context.Context) error {
		var err error
		rec, err = e.users.Create(ctx, coreuser.New{
			Email:        "s05-master@example.com",
			PasswordHash: hash,
			Role:         coreauth.RoleMaster,
		}, 10000)
		return err
	})
	if err != nil {
		t.Fatalf("create master: %v", err)
	}
	resp, body := e.login(rec.Email, "master-password-9!")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("master login = %d, want 200", resp.StatusCode)
	}
	return body.CSRFToken
}

// s05SetRegistration 用默认（已登录 master）客户端写开关。
func s05SetRegistration(t *testing.T, e *authEnv, csrf string, enabled bool) s05Registration {
	t.Helper()
	resp, payload := e.do(http.MethodPut, "/api/auth/registration",
		map[string]any{"enabled": enabled}, map[string]string{"X-CSRF-Token": csrf})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("PUT registration = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	var status s05Registration
	if err := json.Unmarshal(payload, &status); err != nil {
		t.Fatalf("decode registration: %v; body=%q", err, payload)
	}
	return status
}

// 新部署默认不允许公开注册；关闭时提交被拒且无副作用。
func TestS05RegistrationClosedByDefault(t *testing.T) {
	e := newAuthEnv(t)
	status := s05Status(t, e)
	if status.Enabled {
		t.Fatal("a fresh deployment reports registration enabled; want closed")
	}
	if status.TermsVersion < 1 || status.PrivacyVersion < 1 {
		t.Fatalf("agreement versions = %d/%d, want positive", status.TermsVersion, status.PrivacyVersion)
	}

	resp, payload := s05Do(t, e, s05Client(t), http.MethodPost, "/api/auth/register",
		s05RegisterBody("closed@example.com", s05Password), nil)
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("register while closed = %d, want 403; body=%q", resp.StatusCode, payload)
	}
	if code, _ := errorCode(t, payload); code != "forbidden" {
		t.Fatalf("code = %q, want forbidden", code)
	}
	if n := e.scalarInt("SELECT count(*) FROM users WHERE email = 'closed@example.com'"); n != 0 {
		t.Fatalf("closed registration created %d users", n)
	}
	if n := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'auth.register'"); n != 0 {
		t.Fatalf("closed registration wrote %d auth.register rows", n)
	}
}

// 只有 master 能开关；每次写入留 settings.registration 审计。
func TestS05MasterTogglesRegistration(t *testing.T) {
	e := newAuthEnv(t)
	user := e.createUser("s05-plain@example.com", "plain-password-9!")
	plainClient := s05Client(t)
	resp, body := s05Do(t, e, plainClient, http.MethodPost, "/api/auth/login",
		map[string]string{"email": user.Email, "password": "plain-password-9!"}, nil)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("plain login = %d, want 200", resp.StatusCode)
	}
	var plain s05Session
	if err := json.Unmarshal(body, &plain); err != nil {
		t.Fatalf("decode login: %v", err)
	}
	resp, _ = s05Do(t, e, plainClient, http.MethodPut, "/api/auth/registration",
		map[string]any{"enabled": true}, map[string]string{"X-CSRF-Token": plain.CSRFToken})
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("user PUT registration = %d, want 403", resp.StatusCode)
	}
	if s05Status(t, e).Enabled {
		t.Fatal("a non-master write opened registration")
	}

	csrf := s05Master(t, e)
	if got := s05SetRegistration(t, e, csrf, true); !got.Enabled {
		t.Fatal("master did not open registration")
	}
	if !s05Status(t, e).Enabled {
		t.Fatal("public probe still reports closed after the master opened registration")
	}
	if got := s05SetRegistration(t, e, csrf, false); got.Enabled {
		t.Fatal("master did not close registration")
	}
	if n := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'settings.registration'"); n != 2 {
		t.Fatalf("settings.registration rows = %d, want 2", n)
	}
	if detail := e.scalarString(
		"SELECT detail FROM audit_logs WHERE action = 'settings.registration' ORDER BY id DESC LIMIT 1"); detail != `{"field":"registration","to":"disabled"}` {
		t.Fatalf("settings detail = %s, want the disabled transition", detail)
	}
}

// 注册成功即建 user/active 账号并签发会话；口令按 argon2id 存储。
func TestS05RegisterCreatesUserAndSession(t *testing.T) {
	e := newAuthEnv(t)
	csrf := s05Master(t, e)
	s05SetRegistration(t, e, csrf, true)

	client := s05Client(t)
	resp, payload := s05Do(t, e, client, http.MethodPost, "/api/auth/register",
		s05RegisterBody("new-user@example.com", s05Password), nil)
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("register = %d, want 201; body=%q", resp.StatusCode, payload)
	}
	var session s05Session
	if err := json.Unmarshal(payload, &session); err != nil {
		t.Fatalf("decode register body: %v", err)
	}
	if session.Email != "new-user@example.com" || session.Role != "user" || session.Status != "active" {
		t.Fatalf("register body = %+v", session)
	}
	if session.MustChangePassword {
		t.Fatal("a self-chosen password must not force a change")
	}
	if session.CSRFToken == "" {
		t.Fatal("register body has no csrf_token")
	}
	if findCookie(resp, coreauth.InsecureCookieName) == nil {
		t.Fatal("register did not set the session cookie")
	}

	meResp, mePayload := s05Do(t, e, client, http.MethodGet, "/api/auth/me", nil, nil)
	if meResp.StatusCode != http.StatusOK {
		t.Fatalf("me after register = %d, want 200; body=%q", meResp.StatusCode, mePayload)
	}

	if hash := e.scalarString(
		"SELECT password_hash FROM users WHERE email = 'new-user@example.com'"); !strings.HasPrefix(hash, "$argon2id$") {
		t.Fatalf("stored hash = %q, want argon2id", hash)
	}
	if role := e.scalarString(
		"SELECT role::text || '|' || status::text || '|' || must_change_password::text FROM users WHERE email = 'new-user@example.com'"); role != "user|active|false" {
		t.Fatalf("stored user = %q, want user|active|false", role)
	}
	if n := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'auth.register'"); n != 1 {
		t.Fatalf("auth.register rows = %d, want 1", n)
	}
	if n := e.scalarInt(`SELECT count(*) FROM audit_logs
		WHERE action = 'auth.login'
		  AND actor_user_id = (SELECT id FROM users WHERE email = 'new-user@example.com')`); n != 1 {
		t.Fatalf("auth.login rows for the new user = %d, want 1", n)
	}
	label := e.scalarString("SELECT actor_label FROM audit_logs WHERE action = 'auth.register'")
	if label != "n***@example.com" {
		t.Fatalf("register actor label = %q, want the masked email", label)
	}
}

// 校验失败不建用户：弱口令、未同意、陈旧版本、非法邮箱、重复邮箱。
func TestS05RegisterValidation(t *testing.T) {
	e := newAuthEnv(t)
	csrf := s05Master(t, e)
	s05SetRegistration(t, e, csrf, true)

	cases := []struct {
		name   string
		email  string
		mutate func(map[string]any)
		status int
	}{
		{"weak password", "weak@example.com", func(body map[string]any) {
			body["password"] = "short"
		}, http.StatusBadRequest},
		{"no terms", "no-terms@example.com", func(body map[string]any) {
			body["agree_terms"] = false
		}, http.StatusBadRequest},
		{"no privacy", "no-privacy@example.com", func(body map[string]any) {
			body["agree_privacy"] = false
		}, http.StatusBadRequest},
		{"stale terms", "stale-terms@example.com", func(body map[string]any) {
			body["terms_version"] = 2
		}, http.StatusConflict},
		{"stale privacy", "stale-privacy@example.com", func(body map[string]any) {
			body["privacy_version"] = 2
		}, http.StatusConflict},
		{"invalid email", "not-an-email", func(map[string]any) {}, http.StatusBadRequest},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			body := s05RegisterBody(tc.email, s05Password)
			tc.mutate(body)
			resp, payload := s05Do(t, e, s05Client(t), http.MethodPost, "/api/auth/register", body, nil)
			if resp.StatusCode != tc.status {
				t.Fatalf("register = %d, want %d; body=%q", resp.StatusCode, tc.status, payload)
			}
			if n := e.scalarInt("SELECT count(*) FROM users WHERE email = $1", strings.ToLower(tc.email)); n != 0 {
				t.Fatalf("rejected registration created %d users", n)
			}
		})
	}

	// 重复邮箱：第一次成功，第二次冲突；仍只有一行。
	first := s05Client(t)
	if resp, payload := s05Do(t, e, first, http.MethodPost, "/api/auth/register",
		s05RegisterBody("dupe@example.com", s05Password), nil); resp.StatusCode != http.StatusCreated {
		t.Fatalf("first register = %d, want 201; body=%q", resp.StatusCode, payload)
	}
	resp, payload := s05Do(t, e, s05Client(t), http.MethodPost, "/api/auth/register",
		s05RegisterBody("dupe@example.com", s05Password), nil)
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("duplicate register = %d, want 409; body=%q", resp.StatusCode, payload)
	}
	if n := e.scalarInt("SELECT count(*) FROM users WHERE email = 'dupe@example.com'"); n != 1 {
		t.Fatalf("duplicate registration left %d rows, want 1", n)
	}
}

// 同 IP 连续注册受限；跨站浏览器提交被拒。
func TestS05RegisterRateLimitedAndCrossSite(t *testing.T) {
	e := newAuthEnvWith(t, func(cfg *config.Config) {
		cfg.RateLimitEnabled = true
		cfg.RateIPPerMin = 1
		cfg.RateIPBurst = 1
		cfg.RateAccountPerMin = 1
		cfg.RateAccountBurst = 1
	}, nil)
	if _, err := e.db.Exec(e.ctx, "UPDATE settings SET value = 'true' WHERE key = 'registration.enabled'"); err != nil {
		t.Fatalf("open registration: %v", err)
	}

	first, payload := s05Do(t, e, s05Client(t), http.MethodPost, "/api/auth/register",
		s05RegisterBody("rate-one@example.com", s05Password), nil)
	if first.StatusCode != http.StatusCreated {
		t.Fatalf("first register = %d, want 201; body=%q", first.StatusCode, payload)
	}
	second, payload := s05Do(t, e, s05Client(t), http.MethodPost, "/api/auth/register",
		s05RegisterBody("rate-two@example.com", s05Password), nil)
	if second.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("second register = %d, want 429; body=%q", second.StatusCode, payload)
	}
	if second.Header.Get("Retry-After") == "" {
		t.Fatal("429 has no Retry-After")
	}

	// 跨站提交：即使注册开放、限流未命中，也必须 403。
	fresh := newAuthEnv(t)
	if _, err := fresh.db.Exec(fresh.ctx, "UPDATE settings SET value = 'true' WHERE key = 'registration.enabled'"); err != nil {
		t.Fatalf("open registration: %v", err)
	}
	resp, payload := s05Do(t, fresh, s05Client(t), http.MethodPost, "/api/auth/register",
		s05RegisterBody("cross-site@example.com", s05Password),
		map[string]string{"Sec-Fetch-Site": "cross-site"})
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("cross-site register = %d, want 403; body=%q", resp.StatusCode, payload)
	}
	if n := fresh.scalarInt("SELECT count(*) FROM users WHERE email = 'cross-site@example.com'"); n != 0 {
		t.Fatalf("cross-site registration created %d users", n)
	}
}
