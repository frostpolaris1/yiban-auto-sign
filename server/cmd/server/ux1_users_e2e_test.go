package main

// UX-1 用户管理动作 e2e：改角色、重置口令、软删、恢复与 lookup。
//
// # 功能
// 覆盖 v0d 五条验收与安全不变量：最后 master 409、名下账号 409、
// 重置后旧会话立即失效、软删行全端不可见、恢复撞活跃邮箱 409、
// 容量满 429、lookup 角色门槛与静态段遮蔽。
// 走真实栈：真库、真登录、真 cookie、真 HTTP。
// 本文件**不**测性能，**不**复制仓储层用例。
//
// # 归属
// cmd/server 是唯一可同时引用 adapters、app、entry 的位置。
// 环境复用 auth_e2e_test.go 的 newAuthEnv。
//
// # 口径
// 缺 DSN 时整包跳过，并打印横幅。跳过不是绿。
// 每个测试独占一个 schema，互不依赖。
// 手机号只取登记的 138 合成段（tests/synthetic-fixtures.json）。
// 邮箱一律用保留域 example.com。

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/cookiejar"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	appschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/school"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// u1User 是契约 User 形状（含本批新增的 deleted_at）。
type u1User struct {
	ID                 string     `json:"id"`
	Email              string     `json:"email"`
	Role               string     `json:"role"`
	Status             string     `json:"status"`
	MustChangePassword bool       `json:"must_change_password"`
	CreatedAt          time.Time  `json:"created_at"`
	LastLoginAt        *time.Time `json:"last_login_at"`
	DeletedAt          *time.Time `json:"deleted_at"`
}

// u1LookupRef 是 lookup 行的契约形状。
type u1LookupRef struct {
	ID          string `json:"id"`
	EmailMasked string `json:"email_masked"`
	Status      string `json:"status"`
}

// u1Do 用指定客户端发一次请求；csrf 非空时带 X-CSRF-Token。
func u1Do(e *authEnv, client *http.Client, method, path string, body any, csrf string) (*http.Response, []byte) {
	e.t.Helper()
	headers := map[string]string{}
	if csrf != "" {
		headers["X-CSRF-Token"] = csrf
	}
	return e.doAs(client, method, path, body, headers)
}

// u1FreshClient 造一个独立 cookie 客户端，代表另一台设备。
func u1FreshClient(t *testing.T) *http.Client {
	t.Helper()
	jar, err := cookiejar.New(nil)
	if err != nil {
		t.Fatalf("cookie jar: %v", err)
	}
	return &http.Client{Jar: jar, Timeout: 10 * time.Second}
}

// u1LoginOn 用指定客户端登录并返回会话体。失败即终止。
func u1LoginOn(e *authEnv, client *http.Client, email, password string) sessionUserBody {
	e.t.Helper()
	resp, payload := u1Do(e, client, http.MethodPost, "/api/auth/login",
		map[string]string{"email": email, "password": password}, "")
	if resp.StatusCode != http.StatusOK {
		e.t.Fatalf("login %s = %d, want 200; body=%q", email, resp.StatusCode, payload)
	}
	var body sessionUserBody
	if err := json.Unmarshal(payload, &body); err != nil {
		e.t.Fatalf("decode login body: %v; body=%q", err, payload)
	}
	return body
}

// u1AuditActions 按 id 序返回 target 的审计动作。
func u1AuditActions(e *authEnv, targetID string) []string {
	e.t.Helper()
	joined := e.scalarString(
		"SELECT coalesce(string_agg(action, ',' ORDER BY id), '') FROM audit_logs WHERE target_id = $1",
		targetID)
	if joined == "" {
		return nil
	}
	return strings.Split(joined, ",")
}

// u1AuditDetails 拼接 target 某动作的全部 detail。
func u1AuditDetails(e *authEnv, targetID, action string) string {
	e.t.Helper()
	return e.scalarString(
		"SELECT coalesce(string_agg(detail, '|' ORDER BY id), '') FROM audit_logs WHERE target_id = $1 AND action = $2",
		targetID, action)
}

// u1SeedAccount 直接写库造一个账号：学校先建，账号归属 ownerID。
func u1SeedAccount(t *testing.T, e *authEnv, ownerID, phone string) {
	t.Helper()
	schools := pg.NewSchoolRepo(e.db)
	school, err := schools.Create(e.ctx, appschool.New{
		Code: "ux1-" + randomHex(4),
		Name: "UX-1 School " + randomHex(4),
	})
	if err != nil {
		t.Fatalf("seed school: %v", err)
	}
	accounts := pg.NewAccountRepo(e.db)
	err = e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := accounts.Create(ctx, appaccount.New{
			SchoolID:    school.ID,
			OwnerUserID: ownerID,
			Phone:       phone,
		}, 10000, 10000)
		return err
	})
	if err != nil {
		t.Fatalf("seed account %s: %v", phone, err)
	}
}

// u1SeedDeletedUser 写一行带软删标记的用户（哈希用占位串，不登录）。
func u1SeedDeletedUser(t *testing.T, e *authEnv, email, bossID string) coreuser.Record {
	t.Helper()
	var rec coreuser.Record
	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		var err error
		rec, err = e.users.Create(ctx, coreuser.New{
			Email:        email,
			PasswordHash: "placeholder-not-a-real-hash",
			Role:         coreauth.RoleUser,
		}, 10000)
		return err
	})
	if err != nil {
		t.Fatalf("seed user %s: %v", email, err)
	}
	if err := e.users.SoftDelete(e.ctx, rec.ID, bossID, time.Now().UTC()); err != nil {
		t.Fatalf("soft delete %s: %v", email, err)
	}
	rec.DeletedAt = ptrTime(time.Now().UTC())
	return rec
}

func ptrTime(t time.Time) *time.Time { return &t }

// --- 验收①：建号 → 改角色 → 重置 → 软删 → 恢复全链 -------------------------

// 模拟 master 在管理台对一名用户做完整生命周期操作。
// 断言用户可见结果：每步状态码与响应形状；审计动作按序出现；
// 恢复后默认列表含该行；temp_password 不进审计。
func TestUX1G1FullLifecycle(t *testing.T) {
	e := newAuthEnv(t)
	w2dSeedUser(t, e, "g1-boss@example.com", "boss-pass", coreauth.RoleMaster)
	boss := e.w2dLoginAs("g1-boss@example.com", "boss-pass")

	// 建号。
	createResp, createPayload := u1Do(e, e.client, http.MethodPost, "/api/users",
		map[string]any{"email": "g1-target@example.com", "password": "first-pass", "role": "user"},
		boss.CSRFToken)
	if createResp.StatusCode != http.StatusCreated {
		t.Fatalf("create = %d, want 201; body=%q", createResp.StatusCode, createPayload)
	}
	var created u1User
	if err := json.Unmarshal(createPayload, &created); err != nil {
		t.Fatalf("decode create: %v; body=%q", err, createPayload)
	}
	if created.Role != "user" || created.DeletedAt != nil {
		t.Fatalf("created user = %+v", created)
	}

	// 改角色。
	patchResp, patchPayload := u1Do(e, e.client, http.MethodPatch, "/api/users/"+created.ID,
		map[string]any{"role": "admin"}, boss.CSRFToken)
	if patchResp.StatusCode != http.StatusOK {
		t.Fatalf("patch role = %d, want 200; body=%q", patchResp.StatusCode, patchPayload)
	}
	var patched u1User
	if err := json.Unmarshal(patchPayload, &patched); err != nil {
		t.Fatalf("decode patch: %v", err)
	}
	if patched.Role != "admin" {
		t.Fatalf("patched role = %q, want admin", patched.Role)
	}

	// 重置口令。
	resetResp, resetPayload := u1Do(e, e.client, http.MethodPost,
		"/api/users/"+created.ID+"/password-reset", nil, boss.CSRFToken)
	if resetResp.StatusCode != http.StatusOK {
		t.Fatalf("password reset = %d, want 200; body=%q", resetResp.StatusCode, resetPayload)
	}
	var temp struct {
		TempPassword string `json:"temp_password"`
	}
	if err := json.Unmarshal(resetPayload, &temp); err != nil {
		t.Fatalf("decode reset: %v; body=%q", err, resetPayload)
	}
	if temp.TempPassword == "" {
		t.Fatal("password reset returned an empty temp_password")
	}

	// 软删。
	deleteResp, deletePayload := u1Do(e, e.client, http.MethodDelete,
		"/api/users/"+created.ID, nil, boss.CSRFToken)
	if deleteResp.StatusCode != http.StatusNoContent {
		t.Fatalf("delete = %d, want 204; body=%q", deleteResp.StatusCode, deletePayload)
	}

	// 默认列表不含已删行；include_deleted=true 时行内带 deleted_at。
	page, listPayload := e.ulPage("/api/users")
	for _, item := range page.Items {
		if item.ID == created.ID {
			t.Fatalf("deleted user still in the default list; body=%q", listPayload)
		}
	}
	deletedPage := u1ListAll(e, "/api/users?include_deleted=true")
	var found *u1User
	for i := range deletedPage {
		if deletedPage[i].ID == created.ID {
			found = &deletedPage[i]
		}
	}
	if found == nil {
		t.Fatal("include_deleted=true did not return the soft-deleted user")
	}
	if found.DeletedAt == nil {
		t.Fatal("soft-deleted user has a null deleted_at")
	}

	// 恢复。
	restoreResp, restorePayload := u1Do(e, e.client, http.MethodPost,
		"/api/users/"+created.ID+"/restore", nil, boss.CSRFToken)
	if restoreResp.StatusCode != http.StatusOK {
		t.Fatalf("restore = %d, want 200; body=%q", restoreResp.StatusCode, restorePayload)
	}
	var restored u1User
	if err := json.Unmarshal(restorePayload, &restored); err != nil {
		t.Fatalf("decode restore: %v", err)
	}
	if restored.DeletedAt != nil {
		t.Fatalf("restored user still has deleted_at: %+v", restored)
	}
	if restored.Role != "admin" || restored.Status != "active" {
		t.Fatalf("restored user changed shape: %+v", restored)
	}
	after, _ := e.ulPage("/api/users")
	if !containsUserID(after, created.ID) {
		t.Fatal("restored user is missing from the default list")
	}

	// 审计动作按序出现。
	wantOrder := []string{"user.create", "user.role_change", "user.password_reset", "user.delete", "user.restore"}
	gotOrder := u1AuditActions(e, created.ID)
	if strings.Join(gotOrder, ",") != strings.Join(wantOrder, ",") {
		t.Fatalf("audit actions = %v, want %v", gotOrder, wantOrder)
	}
	// temp_password 是单次可见的秘密：绝不进审计 detail。
	if details := u1AuditDetails(e, created.ID, "user.password_reset"); strings.Contains(details, temp.TempPassword) {
		t.Fatalf("audit detail leaked the temp password: %q", details)
	}
}

func containsUserID(page ulPageBody, id string) bool {
	for _, item := range page.Items {
		if item.ID == id {
			return true
		}
	}
	return false
}

// u1ListAll 读一页用户并解码成含 deleted_at 的形状。
func u1ListAll(e *authEnv, path string) []u1User {
	e.t.Helper()
	resp, payload := e.ulGet(path)
	if resp.StatusCode != http.StatusOK {
		e.t.Fatalf("GET %s = %d, want 200; body=%q", path, resp.StatusCode, payload)
	}
	var body struct {
		Items []u1User `json:"items"`
	}
	if err := json.Unmarshal(payload, &body); err != nil {
		e.t.Fatalf("decode list: %v; body=%q", err, payload)
	}
	return body.Items
}

// --- 验收②：最后 master 降级与停用都 409 ----------------------------------

// 模拟 master 试图停掉系统里最后一名管理员。
// 断言用户可见结果：唯一 master 降级 409、停用 409；
// 两名 master 时降级其一 200，此后降级另一名 409。
func TestUX1G2LastMasterIsProtected(t *testing.T) {
	e := newAuthEnv(t)
	w2dSeedUser(t, e, "g2-boss@example.com", "boss-pass", coreauth.RoleMaster)
	w2dSeedUser(t, e, "g2-peer@example.com", "peer-pass", coreauth.RoleMaster)
	boss := e.w2dLoginAs("g2-boss@example.com", "boss-pass")
	// peer 用独立客户端：e.client 的 cookie 罐只保一个会话。
	peerClient := u1FreshClient(t)
	peer := u1LoginOn(e, peerClient, "g2-peer@example.com", "peer-pass")

	// 先降级 peer：boss 仍在，允许。
	demoteResp, demotePayload := u1Do(e, e.client, http.MethodPatch, "/api/users/"+peer.ID,
		map[string]any{"role": "user"}, boss.CSRFToken)
	if demoteResp.StatusCode != http.StatusOK {
		t.Fatalf("demote second master = %d, want 200; body=%q", demoteResp.StatusCode, demotePayload)
	}

	// 此后 boss 是唯一未删 master：降级与停用都 409。
	for _, body := range []map[string]any{
		{"role": "user"},
		{"role": "admin"},
		{"status": "disabled"},
	} {
		resp, payload := u1Do(e, e.client, http.MethodPatch, "/api/users/"+boss.ID,
			body, boss.CSRFToken)
		if resp.StatusCode != http.StatusConflict {
			t.Fatalf("patch %v on the last master = %d, want 409; body=%q", body, resp.StatusCode, payload)
		}
		if code, _ := errorCode(t, payload); code != "conflict" {
			t.Fatalf("patch %v code = %q, want conflict", body, code)
		}
	}
	// 停用无实际发生的判据：再读一次，角色与状态未变。
	final, _ := e.ulPage("/api/users?role=master")
	if len(final.Items) != 1 || final.Items[0].Role != "master" || final.Items[0].Status != "active" {
		t.Fatalf("last master was mutated: %+v", final.Items)
	}
}

// --- 验收③：名下有活跃账号的用户删除 409 ----------------------------------

// 模拟 master 删除一名仍有账号的用户，再清号重删。
// 断言用户可见结果：有账号 409；删号后同一请求 204。
func TestUX1G3DeleteBlockedByOwnedAccounts(t *testing.T) {
	e := newAuthEnv(t)
	w2dSeedUser(t, e, "g3-boss@example.com", "boss-pass", coreauth.RoleMaster)
	w2dSeedUser(t, e, "g3-owner@example.com", "owner-pass", coreauth.RoleUser)
	boss := e.w2dLoginAs("g3-boss@example.com", "boss-pass")

	owner, err := e.users.GetByEmail(e.ctx, "g3-owner@example.com")
	if err != nil {
		t.Fatalf("load owner: %v", err)
	}
	u1SeedAccount(t, e, owner.ID, "13800138010")

	delResp, delPayload := u1Do(e, e.client, http.MethodDelete,
		"/api/users/"+owner.ID, nil, boss.CSRFToken)
	if delResp.StatusCode != http.StatusConflict {
		t.Fatalf("delete user with accounts = %d, want 409; body=%q", delResp.StatusCode, delPayload)
	}
	if code, _ := errorCode(t, delPayload); code != "conflict" {
		t.Fatalf("delete code = %q, want conflict", code)
	}
	if got := e.scalarInt("SELECT count(*) FROM users WHERE id = $1 AND deleted_at IS NULL", owner.ID); got != 1 {
		t.Fatal("the blocked delete still removed the user")
	}

	// 清掉名下账号后同一请求成功。
	if _, err := e.db.Exec(e.ctx, "UPDATE accounts SET deleted_at = now() WHERE owner_user_id = $1", owner.ID); err != nil {
		t.Fatalf("delete accounts: %v", err)
	}
	againResp, againPayload := u1Do(e, e.client, http.MethodDelete,
		"/api/users/"+owner.ID, nil, boss.CSRFToken)
	if againResp.StatusCode != http.StatusNoContent {
		t.Fatalf("delete after clearing accounts = %d, want 204; body=%q", againResp.StatusCode, againPayload)
	}
}

// --- 验收④：重置口令后旧会话立即失效 --------------------------------------

// 模拟目标用户在另一台设备保持登录，master 重置其口令。
// 断言用户可见结果：旧 cookie 立即 401；temp_password 登录成功且
// must_change_password=true；旧口令 401；全部会话行已撤销。
func TestUX1G4PasswordResetRevokesSessions(t *testing.T) {
	e := newAuthEnv(t)
	w2dSeedUser(t, e, "g4-boss@example.com", "boss-pass", coreauth.RoleMaster)
	w2dSeedUser(t, e, "g4-target@example.com", "target-pass", coreauth.RoleUser)
	boss := e.w2dLoginAs("g4-boss@example.com", "boss-pass")

	targetClient := u1FreshClient(t)
	target := u1LoginOn(e, targetClient, "g4-target@example.com", "target-pass")
	meResp, _ := u1Do(e, targetClient, http.MethodGet, "/api/auth/me", nil, "")
	if meResp.StatusCode != http.StatusOK {
		t.Fatalf("target me before reset = %d, want 200", meResp.StatusCode)
	}

	resetResp, resetPayload := u1Do(e, e.client, http.MethodPost,
		"/api/users/"+target.ID+"/password-reset", nil, boss.CSRFToken)
	if resetResp.StatusCode != http.StatusOK {
		t.Fatalf("password reset = %d, want 200; body=%q", resetResp.StatusCode, resetPayload)
	}
	var temp struct {
		TempPassword string `json:"temp_password"`
	}
	if err := json.Unmarshal(resetPayload, &temp); err != nil {
		t.Fatalf("decode reset: %v; body=%q", err, resetPayload)
	}
	if temp.TempPassword == "" {
		t.Fatal("password reset returned an empty temp_password")
	}

	// 旧 cookie 的下一个请求 401。
	staleResp, stalePayload := u1Do(e, targetClient, http.MethodGet, "/api/auth/me", nil, "")
	if staleResp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("me after reset = %d, want 401; body=%q", staleResp.StatusCode, stalePayload)
	}
	// 库内无有效会话行：temp 登录会新建会话，计数必须在它之前。
	if got := e.scalarInt("SELECT count(*) FROM sessions WHERE user_id = $1 AND revoked_at IS NULL", target.ID); got != 0 {
		t.Fatalf("live sessions after reset = %d, want 0", got)
	}
	// temp_password 登录成功，且强制改密。
	tempClient := u1FreshClient(t)
	tempLogin := u1LoginOn(e, tempClient, "g4-target@example.com", temp.TempPassword)
	if !tempLogin.MustChangePassword {
		t.Fatal("temp password login did not set must_change_password")
	}
	// 旧口令失效。
	oldClient := u1FreshClient(t)
	oldResp, _ := u1Do(e, oldClient, http.MethodPost, "/api/auth/login",
		map[string]string{"email": "g4-target@example.com", "password": "target-pass"}, "")
	if oldResp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("old password login = %d, want 401", oldResp.StatusCode)
	}
	// 审计无口令。
	if details := u1AuditDetails(e, target.ID, "user.password_reset"); strings.Contains(details, temp.TempPassword) {
		t.Fatalf("audit leaked temp password: %q", details)
	}
}

// --- 不变量：恢复撞活跃邮箱 409 -------------------------------------------

// 模拟邮箱被活跃用户占用的场景：旧用户被删后邮箱被新建用户拿走。
// 断言用户可见结果：恢复旧用户 409，且旧用户仍处于已删状态。
func TestUX1G5RestoreEmailCollisionIsConflict(t *testing.T) {
	e := newAuthEnv(t)
	w2dSeedUser(t, e, "g5-boss@example.com", "boss-pass", coreauth.RoleMaster)
	boss := e.w2dLoginAs("g5-boss@example.com", "boss-pass")

	old := u1SeedDeletedUser(t, e, "g5-shared@example.com", boss.ID)
	// 同邮箱的活跃新用户：软删行的邮箱可被复用。
	createResp, createPayload := u1Do(e, e.client, http.MethodPost, "/api/users",
		map[string]any{"email": "g5-shared@example.com", "password": "fresh-pass", "role": "user"},
		boss.CSRFToken)
	if createResp.StatusCode != http.StatusCreated {
		t.Fatalf("create reuse = %d, want 201; body=%q", createResp.StatusCode, createPayload)
	}

	restoreResp, restorePayload := u1Do(e, e.client, http.MethodPost,
		"/api/users/"+old.ID+"/restore", nil, boss.CSRFToken)
	if restoreResp.StatusCode != http.StatusConflict {
		t.Fatalf("restore with taken email = %d, want 409; body=%q", restoreResp.StatusCode, restorePayload)
	}
	if code, _ := errorCode(t, restorePayload); code != "conflict" {
		t.Fatalf("restore code = %q, want conflict", code)
	}
	if got := e.scalarInt("SELECT count(*) FROM users WHERE id = $1 AND deleted_at IS NOT NULL", old.ID); got != 1 {
		t.Fatal("the blocked restore revived the user")
	}
}

// --- 不变量：容量满时恢复 429 ----------------------------------------------

// 模拟活跃用户数已达上限后恢复软删行。
// 断言用户可见结果：429 capacity_exceeded，软删行未复活。
func TestUX1G6RestoreAtCapacityIs429(t *testing.T) {
	e := newAuthEnv(t)
	w2dSeedUser(t, e, "g6-boss@example.com", "boss-pass", coreauth.RoleMaster)
	boss := e.w2dLoginAs("g6-boss@example.com", "boss-pass")
	victim := u1SeedDeletedUser(t, e, "g6-victim@example.com", boss.ID)

	// 一条语句灌满上限（10000）：无需 argon2，占位哈希即可。
	if _, err := e.db.Exec(e.ctx, `
		INSERT INTO users (email, password_hash, role)
		SELECT 'g6-filler' || g || '@example.com', 'x', 'user'
		  FROM generate_series(1, 10000) g`); err != nil {
		t.Fatalf("fill capacity: %v", err)
	}

	resp, payload := u1Do(e, e.client, http.MethodPost,
		"/api/users/"+victim.ID+"/restore", nil, boss.CSRFToken)
	if resp.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("restore at capacity = %d, want 429; body=%q", resp.StatusCode, payload)
	}
	if code, _ := errorCode(t, payload); code != "capacity_exceeded" {
		t.Fatalf("restore code = %q, want capacity_exceeded", code)
	}
	if got := e.scalarInt("SELECT count(*) FROM users WHERE id = $1 AND deleted_at IS NOT NULL", victim.ID); got != 1 {
		t.Fatal("the blocked restore revived the user")
	}
}

// --- 不变量：lookup 的角色门槛、形状与静态段遮蔽 ----------------------------

// 模拟 master/admin 在归属选择器里检索用户。
// 断言用户可见结果：admin 200 且只回 id、掩码邮箱、状态；
// user 403；q 缺失 400；PATCH /api/users/lookup 不按 UUID 校验失败。
func TestUX1G7Lookup(t *testing.T) {
	e := newAuthEnv(t)
	w2dSeedUser(t, e, "g7-boss@example.com", "boss-pass", coreauth.RoleMaster)
	w2dSeedUser(t, e, "g7-admin@example.com", "admin-pass", coreauth.RoleAdmin)
	w2dSeedUser(t, e, "g7-user@example.com", "user-pass", coreauth.RoleUser)
	target := w2dSeedUser(t, e, "g7-needle@example.com", "needle-pass", coreauth.RoleUser)
	// 三个角色各用独立客户端：cookie 罐一个会话，不能互相覆盖。
	bossClient := u1FreshClient(t)
	boss := u1LoginOn(e, bossClient, "g7-boss@example.com", "boss-pass")
	adminClient := u1FreshClient(t)
	admin := u1LoginOn(e, adminClient, "g7-admin@example.com", "admin-pass")
	plainClient := u1FreshClient(t)
	plain := u1LoginOn(e, plainClient, "g7-user@example.com", "user-pass")

	// admin 可用；响应只有三个键；无完整邮箱。
	lookupResp, lookupPayload := u1Do(e, adminClient, http.MethodGet, "/api/users/lookup?q=g7-needle", nil, admin.CSRFToken)
	if lookupResp.StatusCode != http.StatusOK {
		t.Fatalf("admin lookup = %d, want 200; body=%q", lookupResp.StatusCode, lookupPayload)
	}
	var page struct {
		Items []u1LookupRef `json:"items"`
	}
	if err := json.Unmarshal(lookupPayload, &page); err != nil {
		t.Fatalf("decode lookup: %v; body=%q", err, lookupPayload)
	}
	if len(page.Items) != 1 || page.Items[0].ID != target.ID {
		t.Fatalf("lookup items = %+v, want exactly the needle", page.Items)
	}
	if masked := page.Items[0].EmailMasked; masked != "g***@example.com" {
		t.Fatalf("masked email = %q, want g***@example.com", masked)
	}
	if strings.Contains(string(lookupPayload), "g7-needle@example.com") {
		t.Fatalf("lookup leaked the full email: %q", lookupPayload)
	}
	var rawItems []map[string]json.RawMessage
	if err := json.Unmarshal(lookupPayload, &struct {
		Items *[]map[string]json.RawMessage `json:"items"`
	}{Items: &rawItems}); err != nil {
		t.Fatalf("decode raw items: %v", err)
	}
	for _, item := range rawItems {
		keys := make([]string, 0, len(item))
		for key := range item {
			keys = append(keys, key)
		}
		if strings.Join(keys, ",") != "email_masked,id,status" {
			t.Fatalf("lookup item keys = %v, want exactly id, email_masked, status", keys)
		}
	}

	// master 也可用。
	if resp, _ := u1Do(e, bossClient, http.MethodGet, "/api/users/lookup?q=g7-needle", nil, boss.CSRFToken); resp.StatusCode != http.StatusOK {
		t.Fatalf("master lookup = %d, want 200", resp.StatusCode)
	}
	// user 403。
	userResp, userPayload := u1Do(e, plainClient, http.MethodGet, "/api/users/lookup?q=g7-needle", nil, plain.CSRFToken)
	if userResp.StatusCode != http.StatusForbidden {
		t.Fatalf("user lookup = %d, want 403; body=%q", userResp.StatusCode, userPayload)
	}
	// q 缺失、空串、超长都是 400。
	for _, path := range []string{
		"/api/users/lookup",
		"/api/users/lookup?q=",
		"/api/users/lookup?q=" + strings.Repeat("x", 254+1),
	} {
		resp, payload := u1Do(e, adminClient, http.MethodGet, path, nil, admin.CSRFToken)
		if resp.StatusCode != http.StatusBadRequest {
			t.Fatalf("GET %s = %d, want 400; body=%q", path, resp.StatusCode, payload)
		}
	}
	// 软删行不可被 lookup 读到。
	if err := e.users.SoftDelete(e.ctx, target.ID, boss.ID, time.Now().UTC()); err != nil {
		t.Fatalf("soft delete needle: %v", err)
	}
	goneResp, gonePayload := u1Do(e, adminClient, http.MethodGet, "/api/users/lookup?q=g7-needle", nil, admin.CSRFToken)
	if goneResp.StatusCode != http.StatusOK {
		t.Fatalf("lookup after delete = %d, want 200; body=%q", goneResp.StatusCode, gonePayload)
	}
	var gonePage struct {
		Items []u1LookupRef `json:"items"`
	}
	if err := json.Unmarshal(gonePayload, &gonePage); err != nil {
		t.Fatalf("decode lookup after delete: %v", err)
	}
	if len(gonePage.Items) != 0 {
		t.Fatalf("soft-deleted user still matches lookup: %+v", gonePage.Items)
	}

	// 静态段遮蔽：PATCH /api/users/lookup 命中用户域的 PATCH 族。
	// 判据：不是把 "lookup" 当 UUID 校验失败的 400；404 或 200 都算遮蔽成立。
	shadowResp, shadowPayload := u1Do(e, bossClient, http.MethodPatch, "/api/users/lookup",
		map[string]any{"role": "admin"}, boss.CSRFToken)
	if shadowResp.StatusCode == http.StatusBadRequest {
		t.Fatalf("PATCH /api/users/lookup = 400 (id parsed as UUID); body=%q", shadowPayload)
	}
}

// --- 不变量：不许重置或删除自己 --------------------------------------------

// 模拟 master 对自己发起重置与删除。
// 断言用户可见结果：两次 403；审计零行；会话与账号原样。
func TestUX1G8SelfResetAndDeleteAreForbidden(t *testing.T) {
	e := newAuthEnv(t)
	w2dSeedUser(t, e, "g8-boss@example.com", "boss-pass", coreauth.RoleMaster)
	boss := e.w2dLoginAs("g8-boss@example.com", "boss-pass")

	resetResp, resetPayload := u1Do(e, e.client, http.MethodPost,
		"/api/users/"+boss.ID+"/password-reset", nil, boss.CSRFToken)
	if resetResp.StatusCode != http.StatusForbidden {
		t.Fatalf("self reset = %d, want 403; body=%q", resetResp.StatusCode, resetPayload)
	}
	if code, _ := errorCode(t, resetPayload); code != "forbidden" {
		t.Fatalf("self reset code = %q, want forbidden", code)
	}
	delResp, delPayload := u1Do(e, e.client, http.MethodDelete, "/api/users/"+boss.ID, nil, boss.CSRFToken)
	if delResp.StatusCode != http.StatusForbidden {
		t.Fatalf("self delete = %d, want 403; body=%q", delResp.StatusCode, delPayload)
	}
	if got := e.scalarInt("SELECT count(*) FROM audit_logs WHERE target_id = $1", boss.ID); got != 0 {
		t.Fatalf("failed self actions wrote %d audit rows, want 0", got)
	}
	meResp, _ := u1Do(e, e.client, http.MethodGet, "/api/auth/me", nil, "")
	if meResp.StatusCode != http.StatusOK {
		t.Fatalf("me after failed self actions = %d, want 200", meResp.StatusCode)
	}
}

// --- 不变量：软删行全端不可见 ----------------------------------------------

// 模拟 master 对已删用户做读写动作。
// 断言用户可见结果：PATCH 与重置都 404；未删用户恢复 404；不存在的 id 404。
func TestUX1G11SoftDeletedIsUnreachable(t *testing.T) {
	e := newAuthEnv(t)
	w2dSeedUser(t, e, "g11-boss@example.com", "boss-pass", coreauth.RoleMaster)
	boss := e.w2dLoginAs("g11-boss@example.com", "boss-pass")
	victim := w2dSeedUser(t, e, "g11-victim@example.com", "victim-pass", coreauth.RoleUser)
	if err := e.users.SoftDelete(e.ctx, victim.ID, boss.ID, time.Now().UTC()); err != nil {
		t.Fatalf("soft delete: %v", err)
	}

	patchResp, patchPayload := u1Do(e, e.client, http.MethodPatch, "/api/users/"+victim.ID,
		map[string]any{"role": "admin"}, boss.CSRFToken)
	if patchResp.StatusCode != http.StatusNotFound {
		t.Fatalf("patch deleted user = %d, want 404; body=%q", patchResp.StatusCode, patchPayload)
	}
	resetResp, resetPayload := u1Do(e, e.client, http.MethodPost,
		"/api/users/"+victim.ID+"/password-reset", nil, boss.CSRFToken)
	if resetResp.StatusCode != http.StatusNotFound {
		t.Fatalf("reset deleted user = %d, want 404; body=%q", resetResp.StatusCode, resetPayload)
	}
	// 未删行恢复 404；不存在的 id 404。
	live := w2dSeedUser(t, e, "g11-live@example.com", "live-pass", coreauth.RoleUser)
	restoreLiveResp, restoreLivePayload := u1Do(e, e.client, http.MethodPost,
		"/api/users/"+live.ID+"/restore", nil, boss.CSRFToken)
	if restoreLiveResp.StatusCode != http.StatusNotFound {
		t.Fatalf("restore live user = %d, want 404; body=%q", restoreLiveResp.StatusCode, restoreLivePayload)
	}
	missingResp, missingPayload := u1Do(e, e.client, http.MethodDelete,
		"/api/users/00000000-0000-0000-0000-000000000000", nil, boss.CSRFToken)
	if missingResp.StatusCode != http.StatusNotFound {
		t.Fatalf("delete missing user = %d, want 404; body=%q", missingResp.StatusCode, missingPayload)
	}

	// 空 PATCH 体：minProperties 判据在用例层，400。
	emptyResp, emptyPayload := u1Do(e, e.client, http.MethodPatch, "/api/users/"+live.ID,
		map[string]any{}, boss.CSRFToken)
	if emptyResp.StatusCode != http.StatusBadRequest {
		t.Fatalf("empty patch = %d, want 400; body=%q", emptyResp.StatusCode, emptyPayload)
	}
	if code, _ := errorCode(t, emptyPayload); code != "validation_failed" {
		t.Fatalf("empty patch code = %q, want validation_failed", code)
	}
}

// --- 不变量：已停用 master 不构成恢复路径（P1-2） --------------------------

// 模拟两名 active master 中 A 停用 B，再试图离开岗位。
// 断言用户可见结果：停用 B 后 B 不能登录（不是恢复路径）；
// A 自降与自停都 409，否则系统只剩一名永远无法登录的 master。
func TestUX1G13DisabledMasterIsNoPath(t *testing.T) {
	e := newAuthEnv(t)
	w2dSeedUser(t, e, "g13-a@example.com", "a-pass", coreauth.RoleMaster)
	w2dSeedUser(t, e, "g13-b@example.com", "b-pass", coreauth.RoleMaster)
	a := e.w2dLoginAs("g13-a@example.com", "a-pass")
	bClient := u1FreshClient(t)
	b := u1LoginOn(e, bClient, "g13-b@example.com", "b-pass")

	// A 停用 B：A 仍是唯一 active master，允许。
	if resp, payload := u1Do(e, e.client, http.MethodPatch, "/api/users/"+b.ID,
		map[string]any{"status": "disabled"}, a.CSRFToken); resp.StatusCode != http.StatusOK {
		t.Fatalf("disable B = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	// B 被停用后不能登录：登录要求 active（app/auth/login.go）。
	bAgain, _ := u1Do(e, bClient, http.MethodPost, "/api/auth/login",
		map[string]string{"email": "g13-b@example.com", "password": "b-pass"}, "")
	if bAgain.StatusCode != http.StatusUnauthorized {
		t.Fatalf("disabled master login = %d, want 401", bAgain.StatusCode)
	}
	// A 自降与自停都 409。
	for _, body := range []map[string]any{
		{"role": "user"},
		{"role": "admin"},
		{"status": "disabled"},
	} {
		resp, payload := u1Do(e, e.client, http.MethodPatch, "/api/users/"+a.ID, body, a.CSRFToken)
		if resp.StatusCode != http.StatusConflict {
			t.Fatalf("patch %v on the last active master = %d, want 409; body=%q",
				body, resp.StatusCode, payload)
		}
	}
}

// --- 不变量：非规范 UUID 不能绕过 self 判据（P1-1） ------------------------

// 模拟唯一 master 用大写形式的自己的 id 发起删除与重置。
// 断言用户可见结果：两次都 403、审计零行、行完好。
// 大写、花括号、URN、32 位无连字符都会被 Postgres 的 ::uuid 定型命中同一行，
// self 判据必须先归一化再比较。
func TestUX1G14SelfBypassViaNonCanonicalUUID(t *testing.T) {
	e := newAuthEnv(t)
	w2dSeedUser(t, e, "g14-boss@example.com", "boss-pass", coreauth.RoleMaster)
	boss := e.w2dLoginAs("g14-boss@example.com", "boss-pass")
	upper := strings.ToUpper(boss.ID)

	delResp, delPayload := u1Do(e, e.client, http.MethodDelete, "/api/users/"+upper, nil, boss.CSRFToken)
	if delResp.StatusCode != http.StatusForbidden {
		t.Fatalf("self delete via uppercase id = %d, want 403; body=%q", delResp.StatusCode, delPayload)
	}
	if code, _ := errorCode(t, delPayload); code != "forbidden" {
		t.Fatalf("self delete code = %q, want forbidden", code)
	}
	resetResp, resetPayload := u1Do(e, e.client, http.MethodPost,
		"/api/users/"+upper+"/password-reset", nil, boss.CSRFToken)
	if resetResp.StatusCode != http.StatusForbidden {
		t.Fatalf("self reset via uppercase id = %d, want 403; body=%q", resetResp.StatusCode, resetPayload)
	}
	if got := e.scalarInt("SELECT count(*) FROM audit_logs WHERE target_id = $1", boss.ID); got != 0 {
		t.Fatalf("bypassed self actions wrote %d audit rows, want 0", got)
	}
	if got := e.scalarInt("SELECT count(*) FROM users WHERE id = $1 AND deleted_at IS NULL", boss.ID); got != 1 {
		t.Fatal("the bypassed self delete still removed the user")
	}
}

// --- 不变量：停用与软删立即杀会话 ------------------------------------------

// 模拟目标用户在线时被停用、再被软删。
// 断言用户可见结果：两次动作后旧 cookie 都立即 401。
func TestUX1G12StatusChangeKillsSessions(t *testing.T) {
	e := newAuthEnv(t)
	w2dSeedUser(t, e, "g12-boss@example.com", "boss-pass", coreauth.RoleMaster)
	w2dSeedUser(t, e, "g12-peer@example.com", "peer-pass", coreauth.RoleMaster)
	w2dSeedUser(t, e, "g12-target@example.com", "target-pass", coreauth.RoleUser)
	boss := e.w2dLoginAs("g12-boss@example.com", "boss-pass")
	target, err := e.users.GetByEmail(e.ctx, "g12-target@example.com")
	if err != nil {
		t.Fatalf("load target: %v", err)
	}

	client := u1FreshClient(t)
	u1LoginOn(e, client, "g12-target@example.com", "target-pass")

	// 停用（target 是 user，不触发最后 master 判据）。
	if resp, payload := u1Do(e, e.client, http.MethodPatch, "/api/users/"+target.ID,
		map[string]any{"status": "disabled"}, boss.CSRFToken); resp.StatusCode != http.StatusOK {
		t.Fatalf("disable = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	meResp, _ := u1Do(e, client, http.MethodGet, "/api/auth/me", nil, "")
	if meResp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("me after disable = %d, want 401", meResp.StatusCode)
	}

	// 软删：解析层 JOIN 判 deleted_at，旧 cookie 仍 401。
	if resp, payload := u1Do(e, e.client, http.MethodDelete, "/api/users/"+target.ID, nil, boss.CSRFToken); resp.StatusCode != http.StatusNoContent {
		t.Fatalf("delete = %d, want 204; body=%q", resp.StatusCode, payload)
	}
	again, _ := u1Do(e, client, http.MethodGet, "/api/auth/me", nil, "")
	if again.StatusCode != http.StatusUnauthorized {
		t.Fatalf("me after delete = %d, want 401", again.StatusCode)
	}
}
