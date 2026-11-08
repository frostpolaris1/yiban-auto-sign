package main

// 登录账号自助注销/恢复的端到端测试。真库、真 HTTP、真 cookie。
//
// # 功能
// 覆盖验收链：注销→登录 401→宽限内恢复→登录 200；错误口令 403 且零写入；
// master 注销被拒；恢复的错误口令/不存在/超窗同一条 401；恢复不建立会话；
// 恢复入口按归一邮箱限流返回 429＋Retry-After。
// 本文件**不**测性能，**不**复制仓储层用例。
//
// # 归属
// cmd/server 是唯一可同时引用 adapters、app、entry 的位置。
// 环境复用 auth_e2e_test.go 的 newAuthEnv；造数复用 ux8 的种子助手。
//
// # 口径
// 缺 DSN 时整包跳过，并打印横幅。跳过不是绿。
// 每个用例独占一个 schema；邮箱只用 example.com 保留域。

import (
	"context"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
	appauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/auth"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
)

// meDeletePath / meRestorePath 是本批的两个端点。
const (
	meDeletePath  = "/api/me/delete"
	meRestorePath = "/api/me/restore"
)

// meLoginStatus 用给定客户端登录并只回状态码。
func meLoginStatus(t *testing.T, e *authEnv, client *http.Client, email, password string) int {
	t.Helper()
	resp, _ := u1Do(e, client, http.MethodPost, "/api/auth/login",
		map[string]string{"email": email, "password": password}, "")
	return resp.StatusCode
}

// TestMeDeleteRestoreChain 覆盖注销→登录 401→宽限内恢复→登录 200 全链。
func TestMeDeleteRestoreChain(t *testing.T) {
	e := newAuthEnv(t)
	user := ux8CreateUser(t, e, "me-chain@example.com", "correct-horse", coreauth.RoleUser)
	account := ux8SeedAccount(t, e, user.ID, "13500135000")

	client := u1FreshClient(t)
	session := u1LoginOn(e, client, "me-chain@example.com", "correct-horse")
	csrf := session.CSRFToken

	// 缺口令 400、错口令 403：二次口令门先于业务写，零副作用。
	resp, payload := u1Do(e, client, http.MethodPost, meDeletePath, nil, csrf)
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("missing current_password = %d, want 400; body=%q", resp.StatusCode, payload)
	}
	resp, payload = u1Do(e, client, http.MethodPost, meDeletePath,
		u1ReauthBody("wrong-pass"), csrf)
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("wrong current_password = %d, want 403; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM users WHERE id = $1::uuid AND deleted_at IS NULL", user.ID); got != 1 {
		t.Fatal("a rejected delete soft-deleted the user")
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NULL", account.ID); got != 1 {
		t.Fatal("a rejected delete soft-deleted the account")
	}
	if n := ux8AuditCount(t, e, "user.self_delete", user.ID); n != 0 {
		t.Fatalf("user.self_delete rows after rejected delete = %d, want 0", n)
	}

	// 正确口令：204，登录账号与名下账号一并软删，全部会话撤销。
	resp, payload = u1Do(e, client, http.MethodPost, meDeletePath,
		u1ReauthBody("correct-horse"), csrf)
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("self delete = %d, want 204; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM users WHERE id = $1::uuid AND deleted_at IS NOT NULL", user.ID); got != 1 {
		t.Fatal("user is not soft-deleted")
	}
	if got := e.scalarString(
		"SELECT coalesce(deleted_by::text, '') FROM accounts WHERE id = $1::uuid", account.ID); got != user.ID {
		t.Fatalf("account deleted_by = %q, want %q", got, user.ID)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NOT NULL", account.ID); got != 1 {
		t.Fatal("cascade account is not soft-deleted")
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM sessions WHERE user_id = $1::uuid AND revoked_at IS NULL", user.ID); got != 0 {
		t.Fatalf("live sessions after self-delete = %d, want 0", got)
	}
	if n := ux8AuditCount(t, e, "user.self_delete", user.ID); n != 1 {
		t.Fatalf("user.self_delete rows = %d, want 1", n)
	}
	if detail := ux8AuditDetails(t, e, "user.self_delete", user.ID); strings.Contains(detail, "me-chain@example.com") {
		t.Fatalf("audit detail carries the email: %q", detail)
	}

	// 注销后：原客户端与重新登录都 401。
	resp, payload = u1Do(e, client, http.MethodGet, "/api/auth/me", nil, "")
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("me after delete = %d, want 401; body=%q", resp.StatusCode, payload)
	}
	if got := meLoginStatus(t, e, client, "me-chain@example.com", "correct-horse"); got != http.StatusUnauthorized {
		t.Fatalf("login after delete = %d, want 401", got)
	}

	// 恢复：错误口令、未知邮箱、正确口令三种请求；前两种同一条 401。
	anon := u1FreshClient(t)
	resp, wrongPayload := u1Do(e, anon, http.MethodPost, meRestorePath,
		map[string]string{"email": "me-chain@example.com", "password": "wrong-pass"}, "")
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("restore wrong password = %d, want 401; body=%q", resp.StatusCode, wrongPayload)
	}
	resp, unknownPayload := u1Do(e, anon, http.MethodPost, meRestorePath,
		map[string]string{"email": "nobody@example.com", "password": "wrong-pass"}, "")
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("restore unknown email = %d, want 401; body=%q", resp.StatusCode, unknownPayload)
	}
	wrongCode, wrongMsg := errorCode(t, wrongPayload)
	unknownCode, unknownMsg := errorCode(t, unknownPayload)
	if wrongCode != unknownCode || wrongMsg != unknownMsg {
		t.Fatalf("restore failures differ: wrong=(%s,%s) unknown=(%s,%s)",
			wrongCode, wrongMsg, unknownCode, unknownMsg)
	}

	resp, payload = u1Do(e, anon, http.MethodPost, meRestorePath,
		map[string]string{"email": "me-chain@example.com", "password": "correct-horse"}, "")
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("restore = %d, want 204; body=%q", resp.StatusCode, payload)
	}
	// 恢复不建立会话：同一客户端读 me 仍 401。
	resp, payload = u1Do(e, anon, http.MethodGet, "/api/auth/me", nil, "")
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("restore established a session: me = %d; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM users WHERE id = $1::uuid AND deleted_at IS NULL", user.ID); got != 1 {
		t.Fatal("user is not live after restore")
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NULL", account.ID); got != 1 {
		t.Fatal("cascade account is not live after restore")
	}
	if n := ux8AuditCount(t, e, "user.self_restore", user.ID); n != 1 {
		t.Fatalf("user.self_restore rows = %d, want 1", n)
	}
	if got := meLoginStatus(t, e, anon, "me-chain@example.com", "correct-horse"); got != http.StatusOK {
		t.Fatalf("login after restore = %d, want 200", got)
	}

	// 已恢复再恢复：同一条 401，不写第二条审计。
	resp, payload = u1Do(e, anon, http.MethodPost, meRestorePath,
		map[string]string{"email": "me-chain@example.com", "password": "correct-horse"}, "")
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("restore live account = %d, want 401; body=%q", resp.StatusCode, payload)
	}
	if n := ux8AuditCount(t, e, "user.self_restore", user.ID); n != 1 {
		t.Fatalf("user.self_restore rows after repeat = %d, want 1", n)
	}
}

// TestMeDeleteRejectsMaster 覆盖 master 自助注销 403 且会话仍有效。
func TestMeDeleteRejectsMaster(t *testing.T) {
	e := newAuthEnv(t)
	boss := ux8CreateUser(t, e, "me-boss@example.com", "correct-horse", coreauth.RoleMaster)
	client := u1FreshClient(t)
	session := u1LoginOn(e, client, "me-boss@example.com", "correct-horse")

	resp, payload := u1Do(e, client, http.MethodPost, meDeletePath,
		u1ReauthBody("correct-horse"), session.CSRFToken)
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("master self delete = %d, want 403; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM users WHERE id = $1::uuid AND deleted_at IS NULL", boss.ID); got != 1 {
		t.Fatal("master was soft-deleted")
	}
	resp, payload = u1Do(e, client, http.MethodGet, "/api/auth/me", nil, "")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("master session after rejected delete = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	if n := ux8AuditCount(t, e, "user.self_delete", boss.ID); n != 0 {
		t.Fatalf("user.self_delete rows for master = %d, want 0", n)
	}
}

// TestMeDeleteReauthThrottled 覆盖连败 5 次后 429＋Retry-After 且零写入。
func TestMeDeleteReauthThrottled(t *testing.T) {
	e := newAuthEnv(t)
	user := ux8CreateUser(t, e, "me-throttle@example.com", "correct-horse", coreauth.RoleUser)
	client := u1FreshClient(t)
	session := u1LoginOn(e, client, "me-throttle@example.com", "correct-horse")

	for i := 0; i < 5; i++ {
		resp, payload := u1Do(e, client, http.MethodPost, meDeletePath,
			u1ReauthBody("wrong-pass"), session.CSRFToken)
		if resp.StatusCode != http.StatusForbidden {
			t.Fatalf("failure %d = %d, want 403; body=%q", i+1, resp.StatusCode, payload)
		}
	}
	resp, payload := u1Do(e, client, http.MethodPost, meDeletePath,
		u1ReauthBody("wrong-pass"), session.CSRFToken)
	if resp.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("throttled delete = %d, want 429; body=%q", resp.StatusCode, payload)
	}
	if retry := resp.Header.Get("Retry-After"); retry == "" {
		t.Fatal("429 carries no Retry-After")
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM users WHERE id = $1::uuid AND deleted_at IS NULL", user.ID); got != 1 {
		t.Fatal("throttled delete soft-deleted the user")
	}
	if n := ux8AuditCount(t, e, "user.self_delete", user.ID); n != 0 {
		t.Fatalf("user.self_delete rows after throttling = %d, want 0", n)
	}
}

// TestRegisterWaitsForUncommittedSelfDelete 钉住 TOCTOU：注销事务未提交时，
// 同邮箱注册必须等锁；提交后注册 409，原主仍能恢复。
func TestRegisterWaitsForUncommittedSelfDelete(t *testing.T) {
	e := newAuthEnv(t)
	user := ux8CreateUser(t, e, "me-race@example.com", "correct-horse", coreauth.RoleUser)
	if _, err := e.db.Exec(e.ctx,
		"UPDATE settings SET value = 'true' WHERE key = 'registration.enabled'"); err != nil {
		t.Fatalf("enable registration: %v", err)
	}

	at := coreclock.System{}.Now().UTC()
	release := make(chan struct{})
	var releaseOnce sync.Once
	defer releaseOnce.Do(func() { close(release) })
	inTx := make(chan struct{})
	txErr := make(chan error, 1)
	go func() {
		txErr <- e.db.InTx(e.ctx, func(ctx context.Context) error {
			if _, err := e.users.SoftDeleteSelf(ctx, user.ID, at); err != nil {
				return err
			}
			close(inTx)
			<-release
			return nil
		})
	}()
	select {
	case <-inTx:
	case err := <-txErr:
		t.Fatalf("soft-delete transaction: %v", err)
	case <-time.After(5 * time.Second):
		t.Fatal("soft-delete transaction did not reach the pause point")
	}
	// 300ms 后放行注销事务；注册必须在这之前一直等待。
	go func() {
		time.Sleep(300 * time.Millisecond)
		releaseOnce.Do(func() { close(release) })
	}()

	body := map[string]any{
		"email": "me-race@example.com", "password": "Correct-horse-9!",
		"agree_terms": true, "agree_privacy": true,
		"terms_version": appauth.TermsVersion, "privacy_version": appauth.PrivacyVersion,
	}
	start := coreclock.System{}.Now()
	resp, payload := u1Do(e, u1FreshClient(t), http.MethodPost, "/api/auth/register", body, "")
	elapsed := time.Since(start)
	if err := <-txErr; err != nil {
		t.Fatalf("soft-delete transaction commit: %v", err)
	}
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("register after uncommitted delete = %d, want 409; body=%q", resp.StatusCode, payload)
	}
	if elapsed < 200*time.Millisecond {
		t.Fatalf("register did not wait for the delete transaction: %s", elapsed)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM users WHERE email = $1 AND deleted_at IS NULL", user.Email); got != 0 {
		t.Fatalf("live users with the email = %d, want 0 before restore", got)
	}

	resp, payload = u1Do(e, u1FreshClient(t), http.MethodPost, meRestorePath,
		map[string]string{"email": "me-race@example.com", "password": "correct-horse"}, "")
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("restore after lost registration race = %d, want 204; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM users WHERE email = $1 AND deleted_at IS NULL", user.Email); got != 1 {
		t.Fatalf("live users with the email = %d, want the restored row", got)
	}
}

// TestMeRestoreBlocksReregistrationDuringGrace 覆盖宽限内邮箱不可被公开注册抢占。
func TestMeRestoreBlocksReregistrationDuringGrace(t *testing.T) {
	e := newAuthEnv(t)
	user := ux8CreateUser(t, e, "me-grace-email@example.com", "correct-horse", coreauth.RoleUser)
	if _, err := e.db.Exec(e.ctx,
		"UPDATE users SET deleted_at = now(), deleted_by = id, updated_at = now() WHERE id = $1::uuid",
		user.ID); err != nil {
		t.Fatalf("mark user deleted: %v", err)
	}
	if _, err := e.db.Exec(e.ctx,
		"UPDATE settings SET value = 'true' WHERE key = 'registration.enabled'"); err != nil {
		t.Fatalf("enable registration: %v", err)
	}
	body := map[string]any{
		"email": "me-grace-email@example.com", "password": "Correct-horse-9!",
		"agree_terms": true, "agree_privacy": true,
		"terms_version": appauth.TermsVersion, "privacy_version": appauth.PrivacyVersion,
	}
	anon := u1FreshClient(t)
	resp, payload := u1Do(e, anon, http.MethodPost, "/api/auth/register", body, "")
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("register within grace = %d, want 409; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarInt("SELECT count(*) FROM users WHERE email = $1", user.Email); got != 1 {
		t.Fatalf("users with the grace email = %d, want 1 (the soft-deleted row)", got)
	}

	// 超窗后邮箱释放，公开注册可以建新行。
	if _, err := e.db.Exec(e.ctx,
		"UPDATE users SET deleted_at = now() - interval '8 days' WHERE id = $1::uuid", user.ID); err != nil {
		t.Fatalf("backdate user delete: %v", err)
	}
	resp, payload = u1Do(e, anon, http.MethodPost, "/api/auth/register", body, "")
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("register after grace = %d, want 201; body=%q", resp.StatusCode, payload)
	}
}

// TestMeRestoreRejectsAdminDeleted 覆盖管理员删除的登录账号不可自助复活。
func TestMeRestoreRejectsAdminDeleted(t *testing.T) {
	e := newAuthEnv(t)
	master := ux8CreateUser(t, e, "me-admin@example.com", "correct-horse", coreauth.RoleMaster)
	target := ux8CreateUser(t, e, "me-admin-target@example.com", "correct-horse", coreauth.RoleUser)

	masterClient := u1FreshClient(t)
	masterSession := u1LoginOn(e, masterClient, "me-admin@example.com", "correct-horse")
	resp, payload := u1Do(e, masterClient, http.MethodDelete, "/api/users/"+target.ID,
		u1ReauthBody("correct-horse"), masterSession.CSRFToken)
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("admin delete user = %d, want 204; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarString(
		"SELECT coalesce(deleted_by::text, '') FROM users WHERE id = $1::uuid", target.ID); got != master.ID {
		t.Fatalf("deleted_by = %q, want admin %q", got, master.ID)
	}

	anon := u1FreshClient(t)
	resp, payload = u1Do(e, anon, http.MethodPost, meRestorePath,
		map[string]string{"email": "me-admin-target@example.com", "password": "correct-horse"}, "")
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("restore admin-deleted login = %d, want 401; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM users WHERE id = $1::uuid AND deleted_at IS NOT NULL", target.ID); got != 1 {
		t.Fatal("admin-deleted login account was revived by self-restore")
	}
}

// TestMeRestoreRejectsExpired 覆盖超 7 天与不存在同一条 401，且行保持软删。
func TestMeRestoreRejectsExpired(t *testing.T) {
	e := newAuthEnv(t)
	user := ux8CreateUser(t, e, "me-expired@example.com", "correct-horse", coreauth.RoleUser)
	at := coreclock.System{}.Now().UTC().Add(-8 * 24 * time.Hour)
	if _, err := e.db.Exec(e.ctx,
		"UPDATE users SET deleted_at = $2, deleted_by = id, updated_at = now() WHERE id = $1::uuid",
		user.ID, at); err != nil {
		t.Fatalf("backdate user delete: %v", err)
	}

	anon := u1FreshClient(t)
	resp, payload := u1Do(e, anon, http.MethodPost, meRestorePath,
		map[string]string{"email": "me-expired@example.com", "password": "correct-horse"}, "")
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("expired restore = %d, want 401; body=%q", resp.StatusCode, payload)
	}
	expiredCode, expiredMsg := errorCode(t, payload)
	resp, payload = u1Do(e, anon, http.MethodPost, meRestorePath,
		map[string]string{"email": "nobody@example.com", "password": "correct-horse"}, "")
	unknownCode, unknownMsg := errorCode(t, payload)
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("unknown restore = %d, want 401; body=%q", resp.StatusCode, payload)
	}
	if expiredCode != unknownCode || expiredMsg != unknownMsg {
		t.Fatalf("expired vs unknown differ: (%s,%s) vs (%s,%s)",
			expiredCode, expiredMsg, unknownCode, unknownMsg)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM users WHERE id = $1::uuid AND deleted_at IS NOT NULL", user.ID); got != 1 {
		t.Fatal("expired restore revived the user")
	}
}

// TestMeRestoreRateLimited 覆盖按归一邮箱的限流：第二次请求 429＋Retry-After。
func TestMeRestoreRateLimited(t *testing.T) {
	e := newAuthEnvWith(t, func(cfg *config.Config) {
		cfg.RateLimitEnabled = true
		cfg.RateIPPerMin = 600
		cfg.RateIPBurst = 600
		cfg.RateAccountPerMin = 1
		cfg.RateAccountBurst = 1
	}, nil)
	user := ux8CreateUser(t, e, "me-limited@example.com", "correct-horse", coreauth.RoleUser)
	if _, err := e.db.Exec(e.ctx,
		"UPDATE users SET deleted_at = now(), deleted_by = id, updated_at = now() WHERE id = $1::uuid",
		user.ID); err != nil {
		t.Fatalf("mark user deleted: %v", err)
	}

	anon := u1FreshClient(t)
	body := map[string]string{"email": "me-limited@example.com", "password": "wrong-pass"}
	resp, payload := u1Do(e, anon, http.MethodPost, meRestorePath, body, "")
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("first restore = %d, want 401; body=%q", resp.StatusCode, payload)
	}
	resp, payload = u1Do(e, anon, http.MethodPost, meRestorePath, body, "")
	if resp.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("second restore = %d, want 429; body=%q", resp.StatusCode, payload)
	}
	if retry := resp.Header.Get("Retry-After"); retry == "" {
		t.Fatal("429 carries no Retry-After")
	}
	if code, _ := errorCode(t, payload); code != "rate_limited" {
		t.Fatalf("429 code = %q, want rate_limited", code)
	}
}
