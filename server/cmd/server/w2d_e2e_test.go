package main

// W2D e2e：POST /api/users（仅 master）。
//
// # 功能
// 覆盖 E-40 至 E-44。走真实栈：真库、真登录、真 cookie。
// 每个用例先由 master 登录，再调建号端点。
// 本文件**不**测性能，**不**复制仓储层用例。
//
// # 归属
// cmd/server 是唯一可同时引用 adapters、app、entry 的位置。
// 环境复用 auth_e2e_test.go 的 newAuthEnv。
//
// # 口径
// 缺 DSN 时整包跳过，并打印横幅。跳过不是绿。
// 建号断言用户可见结果：响应形状、库内口令形态、审计行。
// actor_label 必须已掩码；detail 不得出现邮箱明文。

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"testing"
	"time"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// w2dUserBody 是契约 User 形状的响应体。
type w2dUserBody struct {
	ID                 string     `json:"id"`
	Email              string     `json:"email"`
	Role               string     `json:"role"`
	Status             string     `json:"status"`
	MustChangePassword bool       `json:"must_change_password"`
	CreatedAt          time.Time  `json:"created_at"`
	LastLoginAt        *time.Time `json:"last_login_at"`
}

// w2dSeedUser 直接写库造一个可登录用户。
// 引导 master 与造 admin 都用它：P1 没有注册端点，首个 master 只能这样落地。
func w2dSeedUser(t *testing.T, e *authEnv, email, password string, role coreauth.Role) coreuser.Record {
	t.Helper()
	hash, err := coreauth.HashPassword(password)
	if err != nil {
		t.Fatalf("hash password: %v", err)
	}
	var rec coreuser.Record
	err = e.db.InTx(e.ctx, func(ctx context.Context) error {
		var err error
		rec, err = e.users.Create(ctx, coreuser.New{
			Email:        email,
			PasswordHash: hash,
			Role:         role,
		}, 10000)
		return err
	})
	if err != nil {
		t.Fatalf("create user %s: %v", email, err)
	}
	return rec
}

// w2dCreate 以当前会话调 POST /api/users。
func (e *authEnv) w2dCreate(csrf string, body map[string]any) (*http.Response, []byte) {
	e.t.Helper()
	return e.do(http.MethodPost, "/api/users", body, map[string]string{"X-CSRF-Token": csrf})
}

// w2dLoginAs 登录并返回会话体，失败即终止。
func (e *authEnv) w2dLoginAs(email, password string) sessionUserBody {
	e.t.Helper()
	resp, body := e.login(email, password)
	if resp.StatusCode != http.StatusOK {
		e.t.Fatalf("login %s status = %d, want 200; body=%+v", email, resp.StatusCode, body)
	}
	return body
}

// w2dLiveUsers 数未删除用户。
func (e *authEnv) w2dLiveUsers() int64 {
	e.t.Helper()
	return e.scalarInt("SELECT count(*) FROM users WHERE deleted_at IS NULL")
}

// --- E-40：master 建 user ------------------------------------------------

// 模拟真实管理员：master 登录后填邮箱、口令与角色，提交建号。
// 断言用户可见结果：201、契约形状、库内 argon2id、审计一条且无邮箱明文。
func TestW2DE40MasterCreatesUser(t *testing.T) {
	e := newAuthEnv(t)
	w2dSeedUser(t, e, "boss@example.com", "boss-pass", coreauth.RoleMaster)
	session := e.w2dLoginAs("boss@example.com", "boss-pass")

	// 混合大小写与首尾空白：库内 CHECK 要求 email = lower(email)。
	resp, payload := e.w2dCreate(session.CSRFToken, map[string]any{
		"email":    "  New.User@Example.COM  ",
		"password": "first-pass-123",
		"role":     "user",
	})
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("status = %d, want 201; body=%q", resp.StatusCode, payload)
	}
	var created w2dUserBody
	if err := json.Unmarshal(payload, &created); err != nil {
		t.Fatalf("decode created user: %v; body=%q", err, payload)
	}
	if created.ID == "" {
		t.Fatal("created user has no id")
	}
	if created.Email != "new.user@example.com" {
		t.Fatalf("email = %q, want the normalized lower-case address", created.Email)
	}
	if created.Role != "user" || created.Status != "active" {
		t.Fatalf("role/status = %q/%q, want user/active", created.Role, created.Status)
	}
	if created.MustChangePassword {
		t.Fatal("must_change_password = true, want false")
	}
	if created.CreatedAt.IsZero() {
		t.Fatal("created_at is zero")
	}

	// 库内一行；邮箱已归一。
	if got := e.w2dLiveUsers(); got != 2 {
		t.Fatalf("live users = %d, want 2", got)
	}
	stored := e.scalarString("SELECT email FROM users WHERE id = $1::uuid", created.ID)
	if stored != "new.user@example.com" {
		t.Fatalf("stored email = %q", stored)
	}

	// 口令是 argon2id 哈希，且能校验回原文。
	hash := e.scalarString("SELECT password_hash FROM users WHERE id = $1::uuid", created.ID)
	if !strings.HasPrefix(hash, "$argon2id$") {
		t.Fatalf("password_hash = %q, want an argon2id PHC string", hash)
	}
	if hash == "first-pass-123" {
		t.Fatal("password was stored in clear text")
	}
	check, err := coreauth.VerifyPassword("first-pass-123", hash)
	if err != nil {
		t.Fatalf("verify stored hash: %v", err)
	}
	if !check.Valid {
		t.Fatal("stored hash does not verify against the submitted password")
	}

	// 审计一条：动作、目标、掩码标签，detail 无邮箱。
	if got := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'user.create'"); got != 1 {
		t.Fatalf("user.create audit rows = %d, want 1", got)
	}
	label := e.scalarString("SELECT actor_label FROM audit_logs WHERE action = 'user.create'")
	if label != "b***@example.com" {
		t.Fatalf("actor_label = %q, want the masked email", label)
	}
	targetType := e.scalarString("SELECT target_type FROM audit_logs WHERE action = 'user.create'")
	targetID := e.scalarString("SELECT target_id FROM audit_logs WHERE action = 'user.create'")
	if targetType != "user" || targetID != created.ID {
		t.Fatalf("audit target = %q/%q, want user/%s", targetType, targetID, created.ID)
	}
	detail := e.scalarString("SELECT detail FROM audit_logs WHERE action = 'user.create'")
	if strings.Contains(detail, "new.user@example.com") || strings.Contains(detail, "@") {
		t.Fatalf("audit detail leaked an email: %q", detail)
	}
}

// --- E-41：同一邮箱再建 --------------------------------------------------

// 模拟真实管理员：用大小写变体重复建号。
// 断言用户可见结果：409 conflict，且库内仍只有一行。
func TestW2DE41DuplicateEmailConflicts(t *testing.T) {
	e := newAuthEnv(t)
	w2dSeedUser(t, e, "boss@example.com", "boss-pass", coreauth.RoleMaster)
	session := e.w2dLoginAs("boss@example.com", "boss-pass")

	first, firstPayload := e.w2dCreate(session.CSRFToken, map[string]any{
		"email":    "dana@example.com",
		"password": "dana-pass-123",
		"role":     "admin",
	})
	if first.StatusCode != http.StatusCreated {
		t.Fatalf("first create status = %d, want 201; body=%q", first.StatusCode, firstPayload)
	}
	afterFirst := e.w2dLiveUsers()

	second, secondPayload := e.w2dCreate(session.CSRFToken, map[string]any{
		"email":    "DANA@Example.com",
		"password": "other-pass-123",
		"role":     "user",
	})
	if second.StatusCode != http.StatusConflict {
		t.Fatalf("duplicate status = %d, want 409; body=%q", second.StatusCode, secondPayload)
	}
	if code, _ := errorCode(t, secondPayload); code != "conflict" {
		t.Fatalf("duplicate code = %q, want conflict", code)
	}
	if got := e.w2dLiveUsers(); got != afterFirst {
		t.Fatalf("live users = %d after conflict, want %d", got, afterFirst)
	}
	if got := e.scalarInt("SELECT count(*) FROM users WHERE email = 'dana@example.com'"); got != 1 {
		t.Fatalf("rows for dana = %d, want 1", got)
	}
}

// --- E-42：非 master 建用户 ----------------------------------------------

// 模拟真实管理员与普通用户：两者都尝试建号。
// 断言用户可见结果：403 forbidden，库内不增行。
func TestW2DE42NonMasterIsForbidden(t *testing.T) {
	e := newAuthEnv(t)
	w2dSeedUser(t, e, "boss@example.com", "boss-pass", coreauth.RoleMaster)
	w2dSeedUser(t, e, "ops@example.com", "ops-pass", coreauth.RoleAdmin)
	w2dSeedUser(t, e, "plain@example.com", "plain-pass", coreauth.RoleUser)
	before := e.w2dLiveUsers()

	for _, actor := range []struct{ email, password string }{
		{"ops@example.com", "ops-pass"},
		{"plain@example.com", "plain-pass"},
	} {
		session := e.w2dLoginAs(actor.email, actor.password)
		resp, payload := e.w2dCreate(session.CSRFToken, map[string]any{
			"email":    "sneak@example.com",
			"password": "sneak-pass-123",
			"role":     "user",
		})
		if resp.StatusCode != http.StatusForbidden {
			t.Fatalf("%s create status = %d, want 403; body=%q", actor.email, resp.StatusCode, payload)
		}
		if code, _ := errorCode(t, payload); code != "forbidden" {
			t.Fatalf("%s code = %q, want forbidden", actor.email, code)
		}
	}

	if got := e.w2dLiveUsers(); got != before {
		t.Fatalf("live users = %d, want %d", got, before)
	}
	if got := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'user.create'"); got != 0 {
		t.Fatalf("user.create audit rows = %d, want 0", got)
	}
}

// --- E-43：非法输入 ------------------------------------------------------

// 模拟真实管理员：提交非法角色、空口令与坏邮箱。
// 断言用户可见结果：400 validation_failed，库内零新增。
func TestW2DE43InvalidInputIsRejected(t *testing.T) {
	e := newAuthEnv(t)
	w2dSeedUser(t, e, "boss@example.com", "boss-pass", coreauth.RoleMaster)
	session := e.w2dLoginAs("boss@example.com", "boss-pass")
	before := e.w2dLiveUsers()

	cases := []struct {
		name string
		body map[string]any
	}{
		{"unknown role", map[string]any{"email": "one@example.com", "password": "x-pass-123", "role": "root"}},
		// 缺 role 不得静默落成 user：仓储对空角色有默认值，故这一条只能由用例层拦住。
		{"empty role", map[string]any{"email": "three@example.com", "password": "x-pass-123", "role": ""}},
		{"empty password", map[string]any{"email": "two@example.com", "password": "", "role": "user"}},
		{"email without at", map[string]any{"email": "not-an-email", "password": "x-pass-123", "role": "user"}},
		{"email with space", map[string]any{"email": "bad email@example.com", "password": "x-pass-123", "role": "user"}},
		{"email with zero-width space", map[string]any{"email": "zero\u200bwidth@example.com", "password": "x-pass-123", "role": "user"}},
	}
	for _, tc := range cases {
		resp, payload := e.w2dCreate(session.CSRFToken, tc.body)
		if resp.StatusCode != http.StatusBadRequest {
			t.Fatalf("%s status = %d, want 400; body=%q", tc.name, resp.StatusCode, payload)
		}
		if code, _ := errorCode(t, payload); code != "validation_failed" {
			t.Fatalf("%s code = %q, want validation_failed", tc.name, code)
		}
	}

	if got := e.w2dLiveUsers(); got != before {
		t.Fatalf("live users = %d, want %d", got, before)
	}
	if got := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'user.create'"); got != 0 {
		t.Fatalf("user.create audit rows = %d, want 0", got)
	}
}

// --- E-44：建出的用户能登录 ----------------------------------------------

// 模拟真实管理员建号后，新用户立即登录。
// 断言用户可见结果：登录 200、me 返回同一个身份、初始口令直接可用。
func TestW2DE44CreatedUserCanLogIn(t *testing.T) {
	e := newAuthEnv(t)
	w2dSeedUser(t, e, "boss@example.com", "boss-pass", coreauth.RoleMaster)
	session := e.w2dLoginAs("boss@example.com", "boss-pass")

	resp, payload := e.w2dCreate(session.CSRFToken, map[string]any{
		"email":    "erin@example.com",
		"password": "erin-pass-123",
		"role":     "admin",
	})
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("create status = %d, want 201; body=%q", resp.StatusCode, payload)
	}
	var created w2dUserBody
	if err := json.Unmarshal(payload, &created); err != nil {
		t.Fatalf("decode created user: %v; body=%q", err, payload)
	}

	loginResp, loginBody := e.login("erin@example.com", "erin-pass-123")
	if loginResp.StatusCode != http.StatusOK {
		t.Fatalf("login status = %d, want 200; body=%+v", loginResp.StatusCode, loginBody)
	}
	if loginBody.ID != created.ID || loginBody.Email != "erin@example.com" || loginBody.Role != "admin" {
		t.Fatalf("login body = %+v, want the created identity", loginBody)
	}
	if loginBody.CSRFToken == "" {
		t.Fatal("login body has no csrf_token")
	}

	meResp, mePayload := e.do(http.MethodGet, "/api/auth/me", nil, nil)
	if meResp.StatusCode != http.StatusOK {
		t.Fatalf("me status = %d, want 200; body=%q", meResp.StatusCode, mePayload)
	}
	var me sessionUserBody
	if err := json.Unmarshal(mePayload, &me); err != nil {
		t.Fatalf("decode me: %v; body=%q", err, mePayload)
	}
	if me.ID != created.ID {
		t.Fatalf("me id = %q, want %q", me.ID, created.ID)
	}
}

// P2 回归：本地部含非 ASCII 字符时，建号与登录都必须成功。
// 旧实现按字节切首字符，掩码邮箱产出非法 UTF-8，登录写审计即 500。
func TestW2DE45NonASCIIEmailSurvivesLogin(t *testing.T) {
	e := newAuthEnv(t)
	w2dSeedUser(t, e, "boss@example.com", "boss-pass", coreauth.RoleMaster)
	session := e.w2dLoginAs("boss@example.com", "boss-pass")

	resp, payload := e.w2dCreate(session.CSRFToken, map[string]any{
		"email":    "学生@example.com",
		"password": "non-ascii-pass",
		"role":     "user",
	})
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("status = %d, want 201; body=%q", resp.StatusCode, payload)
	}

	// 真实登录链：审计 actor_label 由掩码邮箱生成，必须能落库。
	login := e.w2dLoginAs("学生@example.com", "non-ascii-pass")
	if login.CSRFToken == "" {
		t.Fatal("login returned no csrf token")
	}
}
