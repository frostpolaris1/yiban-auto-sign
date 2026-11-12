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
	"encoding/json"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
	appauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/auth"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// meDeletePath / meRestorePath 是本批的两个端点。
const (
	meDeletePath  = "/api/me/delete"
	meRestorePath = "/api/me/restore"
)

// errorDetails 解码错误信封的 details 对象。信封四键恒在，
// details 缺省是 {}，故取值前只判键存在。
func errorDetails(t *testing.T, payload []byte) map[string]any {
	t.Helper()
	var env struct {
		Error struct {
			Details map[string]any `json:"details"`
		} `json:"error"`
	}
	if err := json.Unmarshal(payload, &env); err != nil {
		t.Fatalf("decode error envelope: %v; body=%q", err, payload)
	}
	return env.Error.Details
}

// dbDeletedAtUTC 取用户行的 deleted_at，UTC、毫秒精度、ISO 文本，Go 侧解析。
func dbDeletedAtUTC(t *testing.T, e *authEnv, userID string) time.Time {
	t.Helper()
	raw := e.scalarString(`
		SELECT to_char(deleted_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')
		  FROM users WHERE id = $1::uuid`, userID)
	got, err := time.Parse(time.RFC3339, raw)
	if err != nil {
		t.Fatalf("parse deleted_at %q: %v", raw, err)
	}
	return got
}

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

	// 正确口令：200＋恢复窗（clc：注销当下把「7 天内可恢复」回给用户），
	// 登录账号与名下账号一并软删，全部会话撤销。
	resp, payload = u1Do(e, client, http.MethodPost, meDeletePath,
		u1ReauthBody("correct-horse"), csrf)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("self delete = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	var deleteBody struct {
		RecoverUntil string `json:"recover_until"`
	}
	if err := json.Unmarshal(payload, &deleteBody); err != nil {
		t.Fatalf("decode delete body: %v; body=%q", err, payload)
	}
	if deleteBody.RecoverUntil == "" {
		t.Fatalf("delete body carries no recover_until: %q", payload)
	}
	deleteUntil, err := time.Parse(time.RFC3339, deleteBody.RecoverUntil)
	if err != nil {
		t.Fatalf("recover_until %q not RFC3339: %v", deleteBody.RecoverUntil, err)
	}
	if want := dbDeletedAtUTC(t, e, user.ID).Add(coreuser.SelfRestoreGrace); deleteUntil.Sub(want) > time.Second || want.Sub(deleteUntil) > time.Second {
		t.Fatalf("recover_until = %v, want deleted_at+7d = %v (±1s)", deleteUntil, want)
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
	// 冷静期登录（口令对）：401＋details.recoverable＋recover_until，
	// recover_until 与注销响应同值——恢复窗只有一个事实源。
	graceClient := u1FreshClient(t)
	resp, payload = u1Do(e, graceClient, http.MethodPost, "/api/auth/login",
		map[string]string{"email": "me-chain@example.com", "password": "correct-horse"}, "")
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("login after delete = %d, want 401; body=%q", resp.StatusCode, payload)
	}
	loginDetails := errorDetails(t, payload)
	if loginDetails["recoverable"] != true {
		t.Fatalf("grace-window login details = %v, want recoverable=true", loginDetails)
	}
	// 同锚点判据用时刻比较，不用字符串相等：注销响应序列化 Go 时钟原值，
	// 登录引导读库（微秒精度），尾数可差、时刻必须同（±1s）。
	raw, isStr := loginDetails["recover_until"].(string)
	if !isStr {
		t.Fatalf("login details recover_until = %v, want RFC3339 string", loginDetails["recover_until"])
	}
	loginUntil, err := time.Parse(time.RFC3339, raw)
	if err != nil {
		t.Fatalf("login recover_until %q not RFC3339: %v", raw, err)
	}
	if d := loginUntil.Sub(deleteUntil); d > time.Second || d < -time.Second {
		t.Fatalf("login recover_until = %v, want the delete response value %v (±1s)", loginUntil, deleteUntil)
	}
	// 引导登录不建会话：该客户端随后读 me 仍 401。
	resp, payload = u1Do(e, graceClient, http.MethodGet, "/api/auth/me", nil, "")
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("grace-window login established a session: me = %d; body=%q", resp.StatusCode, payload)
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

// TestMeLoginRecoverableWindowShapes 钉住冷静期登录引导的三条谓词：
// 窗内本人软删且口令正确 → 401＋details.recoverable＋recover_until；
// 超窗不带字段、不存在不带字段（同形，防枚举）；窗内口令错也不带
// （对齐旧线 auth.py：recoverable 只在口令验对后出现）。
func TestMeLoginRecoverableWindowShapes(t *testing.T) {
	e := newAuthEnv(t)
	user := ux8CreateUser(t, e, "shape-grace@example.com", "correct-horse", coreauth.RoleUser)
	anon := u1FreshClient(t)
	good := map[string]string{"email": "shape-grace@example.com", "password": "correct-horse"}

	// 基线：不存在用户的 401 形状（code＋message 为同形判据的参照）。
	resp, payload := u1Do(e, anon, http.MethodPost, "/api/auth/login",
		map[string]string{"email": "shape-nobody@example.com", "password": "correct-horse"}, "")
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("unknown login = %d, want 401; body=%q", resp.StatusCode, payload)
	}
	baseCode, baseMessage := errorCode(t, payload)
	if d := errorDetails(t, payload); d["recoverable"] != nil || d["recover_until"] != nil {
		t.Fatalf("unknown login carries recovery fields: %v", d)
	}

	// 注销（SQL 造注销终态，同超窗用例先例）：窗内口令正确带字段。
	if _, err := e.db.Exec(e.ctx, `
		UPDATE users SET deleted_at = now(), deleted_by = id, updated_at = now()
		 WHERE id = $1::uuid`, user.ID); err != nil {
		t.Fatalf("mark user deleted: %v", err)
	}
	resp, payload = u1Do(e, anon, http.MethodPost, "/api/auth/login", good, "")
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("grace login = %d, want 401; body=%q", resp.StatusCode, payload)
	}
	graceCode, graceMessage := errorCode(t, payload)
	if graceCode != baseCode || graceMessage != baseMessage {
		t.Fatalf("grace login shape differs from baseline: (%s,%s) vs (%s,%s)",
			graceCode, graceMessage, baseCode, baseMessage)
	}
	d := errorDetails(t, payload)
	if d["recoverable"] != true {
		t.Fatalf("grace login details = %v, want recoverable=true", d)
	}
	raw, isString := d["recover_until"].(string)
	if !isString {
		t.Fatalf("recover_until = %v, want RFC3339 string", d["recover_until"])
	}
	until, err := time.Parse(time.RFC3339, raw)
	if err != nil {
		t.Fatalf("recover_until %q not RFC3339: %v", raw, err)
	}
	if want := dbDeletedAtUTC(t, e, user.ID).Add(coreuser.SelfRestoreGrace); until.Sub(want) > time.Second || want.Sub(until) > time.Second {
		t.Fatalf("recover_until = %v, want deleted_at+7d = %v (±1s)", until, want)
	}

	// 窗内口令错：不带字段（防枚举，与不存在同形）。
	resp, payload = u1Do(e, anon, http.MethodPost, "/api/auth/login",
		map[string]string{"email": "shape-grace@example.com", "password": "wrong-horse"}, "")
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("wrong-password login = %d, want 401", resp.StatusCode)
	}
	if d := errorDetails(t, payload); d["recoverable"] != nil {
		t.Fatalf("wrong-password login leaks recoverable: %v", d)
	}

	// 超窗：口令正确也不带字段，与不存在同形。
	if _, err := e.db.Exec(e.ctx,
		"UPDATE users SET deleted_at = now() - interval '8 days' WHERE id = $1::uuid", user.ID); err != nil {
		t.Fatalf("backdate user delete: %v", err)
	}
	resp, payload = u1Do(e, anon, http.MethodPost, "/api/auth/login", good, "")
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("expired login = %d, want 401; body=%q", resp.StatusCode, payload)
	}
	expiredCode, expiredMessage := errorCode(t, payload)
	if expiredCode != baseCode || expiredMessage != baseMessage {
		t.Fatalf("expired login shape differs from baseline: (%s,%s) vs (%s,%s)",
			expiredCode, expiredMessage, baseCode, baseMessage)
	}
	if d := errorDetails(t, payload); d["recoverable"] != nil {
		t.Fatalf("expired login leaks recoverable: %v", d)
	}
}

// TestMeRestorePhoneSquattedIs409 钉住恢复撞号语义：A 注销→手机号立即释放
// （与 D7 同口径）→B 同校同号上线→A 恢复撞唯一索引 = 409 conflict＋可懂文案，
// **不**折叠成 401；文案不泄露占用者；事务整体回滚，A 与其账号保持软删；
// 号释放后（B 行软删）A 恢复走通 204。
func TestMeRestorePhoneSquattedIs409(t *testing.T) {
	e := newAuthEnv(t)
	owner := ux8CreateUser(t, e, "sq-owner@example.com", "correct-horse", coreauth.RoleUser)
	squatter := ux8CreateUser(t, e, "sq-squatter@example.com", "correct-horse", coreauth.RoleUser)
	account := ux8SeedAccount(t, e, owner.ID, "13500135000")

	client := u1FreshClient(t)
	session := u1LoginOn(e, client, "sq-owner@example.com", "correct-horse")
	resp, payload := u1Do(e, client, http.MethodPost, meDeletePath,
		u1ReauthBody("correct-horse"), session.CSRFToken)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("self delete = %d, want 200; body=%q", resp.StatusCode, payload)
	}

	// 抢注：注销已释放号，同校同号可上线（直接 SQL，同 ux8SeedAccount 先例）。
	squattedID, err := e.db.QueryString(e.ctx, `
		INSERT INTO accounts (school_id, owner_user_id, phone)
		VALUES ($1::uuid, $2::uuid, $3) RETURNING id::text`,
		account.SchoolID, squatter.ID, account.Phone)
	if err != nil {
		t.Fatalf("squatter insert: %v", err)
	}

	anon := u1FreshClient(t)
	resp, payload = u1Do(e, anon, http.MethodPost, meRestorePath,
		map[string]string{"email": "sq-owner@example.com", "password": "correct-horse"}, "")
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("restore into squatted phone = %d, want 409; body=%q", resp.StatusCode, payload)
	}
	code, message := errorCode(t, payload)
	if code != "conflict" {
		t.Fatalf("409 code = %q, want conflict", code)
	}
	if message == "" || strings.Contains(message, "sq-squatter@example.com") ||
		strings.Contains(message, squatter.ID) || strings.Contains(message, squattedID) {
		t.Fatalf("conflict message leaks the occupant: %q", message)
	}

	// 拒绝即整体回滚：A 与 A 的账号仍是软删，B 的行不受影响，无恢复审计。
	if got := e.scalarInt(
		"SELECT count(*) FROM users WHERE id = $1::uuid AND deleted_at IS NOT NULL", owner.ID); got != 1 {
		t.Fatal("conflict-rejected restore revived the user")
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NOT NULL", account.ID); got != 1 {
		t.Fatal("conflict-rejected restore revived the owner's account")
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NULL", squattedID); got != 1 {
		t.Fatal("the rejected restore touched the squatter's row")
	}
	if n := ux8AuditCount(t, e, "user.self_restore", owner.ID); n != 0 {
		t.Fatalf("user.self_restore rows after 409 = %d, want 0", n)
	}

	// 号释放（B 行软删）后，A 恢复走通——409 是撞号专属，不是一刀切拒绝。
	if _, err := e.db.Exec(e.ctx,
		"UPDATE accounts SET deleted_at = now(), deleted_by = $2::uuid, updated_at = now() WHERE id = $1::uuid",
		squattedID, squatter.ID); err != nil {
		t.Fatalf("release squatted phone: %v", err)
	}
	resp, payload = u1Do(e, anon, http.MethodPost, meRestorePath,
		map[string]string{"email": "sq-owner@example.com", "password": "correct-horse"}, "")
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("restore after phone released = %d, want 204; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NULL", account.ID); got != 1 {
		t.Fatal("owner's account is not live after a successful restore")
	}
	if n := ux8AuditCount(t, e, "user.self_restore", owner.ID); n != 1 {
		t.Fatalf("user.self_restore rows = %d, want 1", n)
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
