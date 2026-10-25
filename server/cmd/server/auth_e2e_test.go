package main

// 认证端点的端到端测试。真库、真 HTTP、真 cookie。
//
// # 功能
// 覆盖 E-20 至 E-25 与「改密撤销旧会话」。
// 本文件**不**测性能，**不**复制仓储层用例。
//
// # 归属
// cmd/server 是唯一可同时引用 adapters、app、entry 的位置。
// 故 HTTP 与真库的组合测试落在这里。
//
// # 口径
// 缺 DSN 时整包跳过，并打印横幅。CI 缺 DSN 即失败。
// 每个测试独占一个 schema，互不依赖。

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/cookiejar"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

const authDSNEnv = "YIBAN_TEST_DATABASE_URL"

// 与 werkzeug scrypt 参数同形的固定向量。
// 向量由 Python hashlib.scrypt 生成，不是本仓代码算出。
const (
	legacyScryptHash = "scrypt:65536:8:1$testsalt12345678$b0e015afcf201322238638333e5680cb8a0cb7ac1e223bb488289c6a6156e3aa5a128ee8f9ba94ef3813684f5237992c248f9beb23f4fc946bcf25278795abf8"
	legacyPassword   = "legacy-pass"
)

var authTestPepper = []byte("0123456789abcdef0123456789abcdef")

// TestMain 让缺库显性可见（AGENTS §30）。
func TestMain(m *testing.M) {
	if strings.TrimSpace(os.Getenv(authDSNEnv)) == "" {
		if strings.TrimSpace(os.Getenv("CI")) != "" {
			fmt.Fprintf(os.Stderr, "\nFAIL: %s is not set in CI. The auth e2e tests "+
				"DID NOT RUN. An unset database is never a green result.\n\n", authDSNEnv)
			os.Exit(1)
		}
		fmt.Fprintf(os.Stdout, "\n"+
			"**********************************************************************\n"+
			"* SKIP: %s is not set.\n"+
			"* The auth e2e tests in cmd/server DID NOT RUN.\n"+
			"* An unset database is never a green result.\n"+
			"**********************************************************************\n\n", authDSNEnv)
	}
	os.Exit(m.Run())
}

type authEnv struct {
	t       *testing.T
	ctx     context.Context
	schema  string
	admin   *pg.DB
	stack   *appStack
	db      *pg.DB
	users   *pg.UserRepo
	resets  *pg.PasswordResetRepo
	client  *http.Client
	baseURL string
	httpSrv *httptest.Server
}

func newAuthEnv(t *testing.T) *authEnv {
	t.Helper()
	base := strings.TrimSpace(os.Getenv(authDSNEnv))
	if base == "" {
		t.Skip("SKIP: " + authDSNEnv + " is not set. Auth e2e DID NOT RUN.")
	}
	ctx := t.Context()
	schema := "authtest_" + randomHex(8)

	admin, err := pg.Open(ctx, pg.Options{DSN: base, MaxConns: 1, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open admin connection: %v", err)
	}
	if _, err := admin.Exec(ctx, "CREATE SCHEMA "+schema); err != nil {
		admin.Close()
		t.Fatalf("create schema %s: %v", schema, err)
	}

	cfg := &config.Config{
		Env:             "test",
		DatabaseURL:     withSearchPath(base, schema),
		InsecureCookies: true,
		HashPepper:      authTestPepper,
	}
	stack, err := buildStack(cfg, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		_, _ = admin.Exec(ctx, "DROP SCHEMA IF EXISTS "+schema+" CASCADE")
		admin.Close()
		t.Fatalf("build stack: %v", err)
	}

	jar, err := cookiejar.New(nil)
	if err != nil {
		t.Fatalf("cookie jar: %v", err)
	}
	srv := httptest.NewServer(stack.engine)
	env := &authEnv{
		t:       t,
		ctx:     ctx,
		schema:  schema,
		admin:   admin,
		stack:   stack,
		db:      stack.db,
		users:   pg.NewUserRepo(stack.db),
		resets:  pg.NewPasswordResetRepo(stack.db),
		client:  &http.Client{Jar: jar, Timeout: 10 * time.Second},
		baseURL: srv.URL,
		httpSrv: srv,
	}
	t.Cleanup(func() {
		srv.Close()
		stack.close()
		dropCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		if _, err := admin.Exec(dropCtx, "DROP SCHEMA IF EXISTS "+schema+" CASCADE"); err != nil {
			t.Logf("drop schema %s: %v", schema, err)
		}
		admin.Close()
	})
	return env
}

func withSearchPath(dsn, schema string) string {
	sep := "?"
	if strings.Contains(dsn, "?") {
		sep = "&"
	}
	return dsn + sep + "search_path=" + schema
}

func randomHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		panic("auth e2e: cannot read random bytes: " + err.Error())
	}
	return hex.EncodeToString(b)
}

func (e *authEnv) createUser(email, password string) coreuser.Record {
	e.t.Helper()
	hash, err := coreauth.HashPassword(password)
	if err != nil {
		e.t.Fatalf("hash password: %v", err)
	}
	return e.createUserWithHash(email, hash)
}

func (e *authEnv) createUserWithHash(email, hash string) coreuser.Record {
	e.t.Helper()
	var rec coreuser.Record
	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		var err error
		rec, err = e.users.Create(ctx, coreuser.New{
			Email:        email,
			PasswordHash: hash,
			Role:         coreauth.RoleUser,
		}, 10000)
		return err
	})
	if err != nil {
		e.t.Fatalf("create user %s: %v", email, err)
	}
	return rec
}

// do 发一次 JSON 请求并读完全部响应体。
func (e *authEnv) do(method, path string, body any, headers map[string]string) (*http.Response, []byte) {
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
	resp, err := e.client.Do(req)
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

type sessionUserBody struct {
	ID                 string `json:"id"`
	Email              string `json:"email"`
	Role               string `json:"role"`
	Status             string `json:"status"`
	MustChangePassword bool   `json:"must_change_password"`
	CSRFToken          string `json:"csrf_token"`
}

func (e *authEnv) login(email, password string) (*http.Response, sessionUserBody) {
	e.t.Helper()
	resp, payload := e.do(http.MethodPost, "/api/auth/login",
		map[string]string{"email": email, "password": password}, nil)
	var body sessionUserBody
	if resp.StatusCode == http.StatusOK {
		if err := json.Unmarshal(payload, &body); err != nil {
			e.t.Fatalf("decode login body: %v; body=%q", err, payload)
		}
	}
	return resp, body
}

func (e *authEnv) scalarInt(sql string, args ...any) int64 {
	e.t.Helper()
	v, err := e.db.QueryInt64(e.ctx, sql, args...)
	if err != nil {
		e.t.Fatalf("query %q: %v", sql, err)
	}
	return v
}

func (e *authEnv) scalarString(sql string, args ...any) string {
	e.t.Helper()
	v, err := e.db.QueryString(e.ctx, sql, args...)
	if err != nil {
		e.t.Fatalf("query %q: %v", sql, err)
	}
	return v
}

func errorCode(t *testing.T, payload []byte) (code, message string) {
	t.Helper()
	var env struct {
		Error struct {
			Code    string `json:"code"`
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.Unmarshal(payload, &env); err != nil {
		t.Fatalf("decode error envelope: %v; body=%q", err, payload)
	}
	return env.Error.Code, env.Error.Message
}

// --- E-20：正确口令登录 ------------------------------------------------

// 模拟真实用户：输入邮箱与口令，拿到会话 cookie，然后读自己的资料。
// 断言用户可见结果：登录 200、me 可用、Cookie 是 HttpOnly 且 SameSite=Lax。
func TestE20LoginWithCorrectPassword(t *testing.T) {
	e := newAuthEnv(t)
	user := e.createUser("alice@example.com", "correct-horse")

	resp, body := e.login("alice@example.com", "correct-horse")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("login status = %d, want 200; body=%+v", resp.StatusCode, body)
	}
	if body.ID != user.ID || body.Email != "alice@example.com" || body.Role != "user" {
		t.Fatalf("login body = %+v", body)
	}
	if body.CSRFToken == "" {
		t.Fatal("login body has no csrf_token")
	}

	cookie := findCookie(resp, coreauth.InsecureCookieName)
	if cookie == nil {
		t.Fatalf("login response has no %s cookie", coreauth.InsecureCookieName)
	}
	if !cookie.HttpOnly {
		t.Fatal("session cookie is not HttpOnly")
	}
	if cookie.Secure {
		t.Fatal("development session cookie must not set Secure")
	}
	if cookie.SameSite != http.SameSiteLaxMode {
		t.Fatalf("cookie SameSite = %v, want Lax", cookie.SameSite)
	}
	if cookie.Path != "/" {
		t.Fatalf("cookie Path = %q, want /", cookie.Path)
	}
	if cookie.Value == "" {
		t.Fatal("session cookie value is empty")
	}

	meResp, mePayload := e.do(http.MethodGet, "/api/auth/me", nil, nil)
	if meResp.StatusCode != http.StatusOK {
		t.Fatalf("me status = %d, want 200; body=%q", meResp.StatusCode, mePayload)
	}
	var me sessionUserBody
	if err := json.Unmarshal(mePayload, &me); err != nil {
		t.Fatalf("decode me: %v", err)
	}
	if me.Email != "alice@example.com" {
		t.Fatalf("me email = %q", me.Email)
	}
	if strings.Contains(string(mePayload), cookie.Value) {
		t.Fatal("me response leaked the session token")
	}

	if got := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'auth.login'"); got != 1 {
		t.Fatalf("auth.login audit rows = %d, want 1", got)
	}
	label := e.scalarString("SELECT actor_label FROM audit_logs WHERE action = 'auth.login'")
	if label != "a***@example.com" {
		t.Fatalf("actor_label = %q, want the masked email", label)
	}
}

func findCookie(resp *http.Response, name string) *http.Cookie {
	for _, c := range resp.Cookies() {
		if c.Name == name {
			return c
		}
	}
	return nil
}

// --- E-21：错误口令与未知用户不可区分 ----------------------------------

// 模拟真实用户：先输错口令，再用不存在的邮箱登录。
// 断言用户可见结果：两次 401、同文案；审计留痕且详情无邮箱。
func TestE21LoginFailuresAreIndistinguishable(t *testing.T) {
	e := newAuthEnv(t)
	e.createUser("bob@example.com", "correct-horse")

	wrongResp, wrongPayload := e.do(http.MethodPost, "/api/auth/login",
		map[string]string{"email": "bob@example.com", "password": "wrong"}, nil)
	unknownResp, unknownPayload := e.do(http.MethodPost, "/api/auth/login",
		map[string]string{"email": "nobody@example.com", "password": "wrong"}, nil)

	if wrongResp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("wrong password status = %d, want 401; body=%q", wrongResp.StatusCode, wrongPayload)
	}
	if unknownResp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("unknown user status = %d, want 401; body=%q", unknownResp.StatusCode, unknownPayload)
	}
	wrongCode, wrongMessage := errorCode(t, wrongPayload)
	unknownCode, unknownMessage := errorCode(t, unknownPayload)
	if wrongCode != "unauthorized" || unknownCode != "unauthorized" {
		t.Fatalf("codes = %q and %q, want unauthorized", wrongCode, unknownCode)
	}
	if wrongMessage != unknownMessage {
		t.Fatalf("messages differ: %q and %q", wrongMessage, unknownMessage)
	}

	if got := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'auth.login_failed'"); got != 2 {
		t.Fatalf("auth.login_failed rows = %d, want 2", got)
	}
	details := e.scalarString(
		"SELECT coalesce(string_agg(detail, '|' ORDER BY id), '') FROM audit_logs WHERE action = 'auth.login_failed'")
	if strings.Contains(details, "bob@example.com") || strings.Contains(details, "nobody@example.com") {
		t.Fatalf("audit detail leaked an email: %q", details)
	}
	if strings.Contains(details, "@") {
		t.Fatalf("audit detail contains an address-like value: %q", details)
	}
}

// --- E-22：带会话但无 CSRF 的登出 --------------------------------------

// 模拟真实用户：登录后用不带 X-CSRF-Token 的请求登出。
// 断言用户可见结果：403，且会话仍然有效。
func TestE22LogoutWithoutCSRFIsForbidden(t *testing.T) {
	e := newAuthEnv(t)
	e.createUser("carol@example.com", "correct-horse")
	resp, _ := e.login("carol@example.com", "correct-horse")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("login status = %d", resp.StatusCode)
	}

	logoutResp, payload := e.do(http.MethodPost, "/api/auth/logout", nil, nil)
	if logoutResp.StatusCode != http.StatusForbidden {
		t.Fatalf("logout without CSRF status = %d, want 403; body=%q", logoutResp.StatusCode, payload)
	}
	if code, _ := errorCode(t, payload); code != "forbidden" {
		t.Fatalf("logout error code = %q, want forbidden", code)
	}
	if got := e.scalarInt("SELECT count(*) FROM sessions WHERE revoked_at IS NOT NULL"); got != 0 {
		t.Fatalf("rejected logout revoked %d sessions", got)
	}
}

// --- E-23：登出后会话不可用 --------------------------------------------

// 模拟真实用户：登录、带 CSRF 登出，再读自己的资料。
// 断言用户可见结果：登出 204、me 401、会话行已撤销。
func TestE23LogoutRevokesSession(t *testing.T) {
	e := newAuthEnv(t)
	e.createUser("dave@example.com", "correct-horse")
	_, body := e.login("dave@example.com", "correct-horse")
	if body.CSRFToken == "" {
		t.Fatal("login returned no CSRF token")
	}

	logoutResp, logoutPayload := e.do(http.MethodPost, "/api/auth/logout", nil,
		map[string]string{"X-CSRF-Token": body.CSRFToken})
	if logoutResp.StatusCode != http.StatusNoContent {
		t.Fatalf("logout status = %d, want 204; body=%q", logoutResp.StatusCode, logoutPayload)
	}
	meResp, mePayload := e.do(http.MethodGet, "/api/auth/me", nil, nil)
	if meResp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("me after logout status = %d, want 401; body=%q", meResp.StatusCode, mePayload)
	}
	if got := e.scalarInt("SELECT count(*) FROM sessions WHERE revoked_at IS NOT NULL"); got != 1 {
		t.Fatalf("revoked session rows = %d, want 1", got)
	}
}

// --- E-24：重置令牌只能使用一次 ----------------------------------------

// 模拟真实用户：用重置令牌改密，再用同一令牌改第二次。
// 断言用户可见结果：第一次 204，第二次 401，第二次未改口令。
func TestE24ResetTokenIsSingleUse(t *testing.T) {
	e := newAuthEnv(t)
	user := e.createUser("erin@example.com", "correct-horse")
	token := "reset-once-token-0123456789"
	now := time.Now().UTC()
	if _, err := e.resets.Create(e.ctx, coreauth.NewPasswordReset{
		UserID:    user.ID,
		TokenHash: coreauth.HashToken(token),
		Now:       now,
		ExpiresAt: now.Add(30 * time.Minute),
	}); err != nil {
		t.Fatalf("create reset token: %v", err)
	}

	firstResp, firstPayload := e.do(http.MethodPost, "/api/auth/reset-password",
		map[string]string{"token": token, "new_password": "first-new-password"}, nil)
	if firstResp.StatusCode != http.StatusNoContent {
		t.Fatalf("first reset status = %d, want 204; body=%q", firstResp.StatusCode, firstPayload)
	}
	hashAfterFirst := e.scalarString("SELECT password_hash FROM users WHERE id = $1", user.ID)
	if !strings.HasPrefix(hashAfterFirst, "$argon2id$") {
		t.Fatalf("password_hash after reset = %q, want argon2id", hashAfterFirst)
	}

	secondResp, secondPayload := e.do(http.MethodPost, "/api/auth/reset-password",
		map[string]string{"token": token, "new_password": "second-new-password"}, nil)
	if secondResp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("second reset status = %d, want 401; body=%q", secondResp.StatusCode, secondPayload)
	}
	if code, _ := errorCode(t, secondPayload); code != "unauthorized" {
		t.Fatalf("second reset code = %q, want unauthorized", code)
	}
	if got := e.scalarString("SELECT password_hash FROM users WHERE id = $1", user.ID); got != hashAfterFirst {
		t.Fatal("a reused reset token changed the password")
	}
	if got := e.scalarInt("SELECT count(*) FROM password_resets WHERE used_at IS NOT NULL"); got != 1 {
		t.Fatalf("used reset rows = %d, want 1", got)
	}
}

// --- E-25：旧 scrypt 用户登录后透明升级 --------------------------------

// 模拟真实用户：旧库用户用原口令登录。
// 断言用户可见结果：登录成功，库内变为 argon2id，再次登录仍成功。
func TestE25LegacyScryptIsUpgraded(t *testing.T) {
	e := newAuthEnv(t)
	user := e.createUserWithHash("frank@example.com", legacyScryptHash)

	resp, body := e.login("frank@example.com", legacyPassword)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("legacy login status = %d, want 200", resp.StatusCode)
	}
	if body.ID != user.ID {
		t.Fatalf("legacy login returned user %s, want %s", body.ID, user.ID)
	}
	upgraded := e.scalarString("SELECT password_hash FROM users WHERE id = $1", user.ID)
	if !strings.HasPrefix(upgraded, "$argon2id$") {
		t.Fatalf("password_hash after login = %q, want argon2id", upgraded)
	}
	if upgraded == legacyScryptHash {
		t.Fatal("password_hash was not rewritten")
	}

	again, _ := e.login("frank@example.com", legacyPassword)
	if again.StatusCode != http.StatusOK {
		t.Fatalf("second login status = %d, want 200", again.StatusCode)
	}
}

// --- 口令重置撤销全部旧会话 --------------------------------------------

// 模拟真实用户：在一台设备登录，然后用重置令牌改密。
// 断言用户可见结果：旧 cookie 立即失效，会话行已撤销。
func TestResetRevokesEverySession(t *testing.T) {
	e := newAuthEnv(t)
	user := e.createUser("grace@example.com", "correct-horse")
	loginResp, _ := e.login("grace@example.com", "correct-horse")
	if loginResp.StatusCode != http.StatusOK {
		t.Fatalf("login status = %d", loginResp.StatusCode)
	}

	token := "reset-revoke-token-0123456789"
	now := time.Now().UTC()
	if _, err := e.resets.Create(e.ctx, coreauth.NewPasswordReset{
		UserID:    user.ID,
		TokenHash: coreauth.HashToken(token),
		Now:       now,
		ExpiresAt: now.Add(30 * time.Minute),
	}); err != nil {
		t.Fatalf("create reset token: %v", err)
	}
	resetResp, payload := e.do(http.MethodPost, "/api/auth/reset-password",
		map[string]string{"token": token, "new_password": "after-reset-password"}, nil)
	if resetResp.StatusCode != http.StatusNoContent {
		t.Fatalf("reset status = %d, want 204; body=%q", resetResp.StatusCode, payload)
	}

	meResp, mePayload := e.do(http.MethodGet, "/api/auth/me", nil, nil)
	if meResp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("me after reset status = %d, want 401; body=%q", meResp.StatusCode, mePayload)
	}
	if got := e.scalarInt("SELECT count(*) FROM sessions WHERE user_id = $1 AND revoked_at IS NULL", user.ID); got != 0 {
		t.Fatalf("live session rows after reset = %d, want 0", got)
	}
	if got := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'auth.password_reset'"); got != 1 {
		t.Fatalf("auth.password_reset rows = %d, want 1", got)
	}
}

// --- 忘记口令不泄露账号是否存在 ----------------------------------------

// 模拟真实用户：分别对已知与未知邮箱发起重置。
// 断言用户可见结果：两次都 204；只有已知账号产生令牌行。
func TestForgotPasswordDoesNotRevealExistence(t *testing.T) {
	e := newAuthEnv(t)
	e.createUser("heidi@example.com", "correct-horse")

	knownResp, knownPayload := e.do(http.MethodPost, "/api/auth/forgot-password",
		map[string]string{"email": "heidi@example.com"}, nil)
	unknownResp, unknownPayload := e.do(http.MethodPost, "/api/auth/forgot-password",
		map[string]string{"email": "absent@example.com"}, nil)

	if knownResp.StatusCode != http.StatusNoContent || unknownResp.StatusCode != http.StatusNoContent {
		t.Fatalf("forgot status = %d and %d, want 204; bodies=%q / %q",
			knownResp.StatusCode, unknownResp.StatusCode, knownPayload, unknownPayload)
	}
	if got := e.scalarInt("SELECT count(*) FROM password_resets"); got != 1 {
		t.Fatalf("reset rows = %d, want 1", got)
	}
	if got := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action LIKE 'auth.%'"); got != 0 {
		t.Fatalf("forgot-password wrote %d auth audit rows; the action is not in the vocabulary", got)
	}
}
