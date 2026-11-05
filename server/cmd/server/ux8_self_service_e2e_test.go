package main

// UX-8 自助面 e2e：账号注销/撤销注销与签到失败邮件开关。
//
// # 功能
// 覆盖 5mx 的验收与安全不变量：注销→恢复全链、非本人 404、
// 管理员删除的账号恢复 403、超 7 天 409、已有生效账号 400、
// 已删除再删 400、未登录 401、三角色自助矩阵、邮件开关默认 true
// 与 user.mail_notify 审计（不含邮箱原文）。
// 走真实栈：真库、真登录、真 cookie、真 HTTP。
// 本文件**不**测性能，**不**复制仓储层用例。
//
// # 归属
// cmd/server 是唯一可同时引用 adapters、app、entry 的位置。
// 环境复用 auth_e2e_test.go 的 newAuthEnv。
//
// # 口径
// 缺 DSN 时整包跳过，并打印横幅。跳过不是绿。
// 每个测试独占一个 schema；手机号只用 synthetic-fixtures 登记值，
// 邮箱一律用保留域 example.com。

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	appschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/school"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// ux8CreateUser 建一个指定角色的用户。角色在登录前写入，会话按新角色解析。
func ux8CreateUser(t *testing.T, e *authEnv, email, password string, role coreauth.Role) coreuser.Record {
	t.Helper()
	rec := e.createUser(email, password)
	if role != coreauth.RoleUser {
		if _, err := e.db.Exec(e.ctx,
			"UPDATE users SET role = $2::user_role WHERE id = $1::uuid", rec.ID, string(role)); err != nil {
			t.Fatalf("set role %s: %v", role, err)
		}
	}
	return rec
}

// ux8SeedAccount 造一个账号并返回行：学校先建，账号归属 ownerID。
func ux8SeedAccount(t *testing.T, e *authEnv, ownerID, phone string) appaccount.Record {
	t.Helper()
	schools := pg.NewSchoolRepo(e.db)
	school, err := schools.Create(e.ctx, appschool.New{
		Code: "ux8-" + randomHex(4),
		Name: "UX-8 School " + randomHex(4),
	})
	if err != nil {
		t.Fatalf("seed school: %v", err)
	}
	accounts := pg.NewAccountRepo(e.db)
	var record appaccount.Record
	err = e.db.InTx(e.ctx, func(ctx context.Context) error {
		var err error
		record, err = accounts.Create(ctx, appaccount.New{
			SchoolID:    school.ID,
			OwnerUserID: ownerID,
			Phone:       phone,
		}, 10000, 10000)
		return err
	})
	if err != nil {
		t.Fatalf("seed account %s: %v", phone, err)
	}
	return record
}

// ux8SoftDelete 以 byID 的身份软删账号，模拟管理员删除。
func ux8SoftDelete(t *testing.T, e *authEnv, accountID, byID string) {
	t.Helper()
	accounts := pg.NewAccountRepo(e.db)
	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		return accounts.SoftDelete(ctx, corescope.Master(), accountID, byID, coreclock.System{}.Now().UTC())
	})
	if err != nil {
		t.Fatalf("soft delete %s: %v", accountID, err)
	}
}

// ux8BackdateDelete 把 deleted_at 推到 days 天前，保留 deleted_by。
func ux8BackdateDelete(t *testing.T, e *authEnv, accountID, byID string, days int) {
	t.Helper()
	at := coreclock.System{}.Now().UTC().Add(-time.Duration(days) * 24 * time.Hour)
	if _, err := e.db.Exec(e.ctx, `
		UPDATE accounts SET deleted_at = $2, deleted_by = $3::uuid, updated_at = now()
		 WHERE id = $1::uuid`, accountID, at, byID); err != nil {
		t.Fatalf("backdate delete: %v", err)
	}
}

// ux8AuditCount 数某 target 的某审计动作行数。
func ux8AuditCount(t *testing.T, e *authEnv, action, targetID string) int64 {
	t.Helper()
	return e.scalarInt(
		"SELECT count(*) FROM audit_logs WHERE action = $1 AND target_id = $2", action, targetID)
}

// ux8AuditDetails 取某 target 的某动作 detail，按 id 序用 | 连接。
func ux8AuditDetails(t *testing.T, e *authEnv, action, targetID string) string {
	t.Helper()
	return e.scalarString(
		"SELECT coalesce(string_agg(detail, '|' ORDER BY id), '') FROM audit_logs WHERE action = $1 AND target_id = $2",
		action, targetID)
}

// ux8AccountBody 是恢复响应的契约形状。
type ux8AccountBody struct {
	ID        string     `json:"id"`
	Status    string     `json:"status"`
	DeletedAt *time.Time `json:"deleted_at"`
}

// ux8MailNotifyBody 是开关响应的契约形状。
type ux8MailNotifyBody struct {
	Enabled bool `json:"enabled"`
}

// TestUX8SelfDeleteRestore 覆盖本人注销→恢复全链与重复动作的负向。
func TestUX8SelfDeleteRestore(t *testing.T) {
	e := newAuthEnv(t)
	user := ux8CreateUser(t, e, "ux8-chain@example.com", "correct-horse", coreauth.RoleUser)
	account := ux8SeedAccount(t, e, user.ID, "13500135000")

	client := u1FreshClient(t)
	session := u1LoginOn(e, client, "ux8-chain@example.com", "correct-horse")
	csrf := session.CSRFToken

	resp, payload := u1Do(e, client, http.MethodPost,
		"/api/me/accounts/"+account.ID+"/delete", u1ReauthBody("correct-horse"), csrf)
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("self delete = %d, want 204; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarString(
		"SELECT coalesce(deleted_by::text, '') FROM accounts WHERE id = $1::uuid", account.ID); got != user.ID {
		t.Fatalf("deleted_by = %q, want %q", got, user.ID)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NOT NULL", account.ID); got != 1 {
		t.Fatal("account is not soft-deleted")
	}
	if n := ux8AuditCount(t, e, "account.self_delete", account.ID); n != 1 {
		t.Fatalf("account.self_delete rows = %d, want 1", n)
	}
	detail := ux8AuditDetails(t, e, "account.self_delete", account.ID)
	if strings.Contains(detail, "13500135000") || strings.Contains(detail, "ux8-chain@example.com") {
		t.Fatalf("audit detail carries PII: %q", detail)
	}

	// 已删除再删 400，且不新增软删审计。
	resp, payload = u1Do(e, client, http.MethodPost,
		"/api/me/accounts/"+account.ID+"/delete", nil, csrf)
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("delete deleted account = %d, want 400; body=%q", resp.StatusCode, payload)
	}
	if n := ux8AuditCount(t, e, "account.self_delete", account.ID); n != 1 {
		t.Fatalf("account.self_delete rows after repeat = %d, want 1", n)
	}

	// 恢复：仓行清空 deleted_at/deleted_by，响应带真实 null。
	resp, payload = u1Do(e, client, http.MethodPost,
		"/api/me/accounts/"+account.ID+"/restore", nil, csrf)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("self restore = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	var restored ux8AccountBody
	if err := json.Unmarshal(payload, &restored); err != nil {
		t.Fatalf("decode restore body: %v; body=%q", err, payload)
	}
	if restored.ID != account.ID || restored.DeletedAt != nil {
		t.Fatalf("restored body = %+v, want id=%s deleted_at=null", restored, account.ID)
	}
	if got := e.scalarString(
		"SELECT coalesce(deleted_by::text, '') FROM accounts WHERE id = $1::uuid", account.ID); got != "" {
		t.Fatalf("deleted_by after restore = %q, want empty", got)
	}
	if n := ux8AuditCount(t, e, "account.self_restore", account.ID); n != 1 {
		t.Fatalf("account.self_restore rows = %d, want 1", n)
	}

	// 未删再恢复 409。
	resp, payload = u1Do(e, client, http.MethodPost,
		"/api/me/accounts/"+account.ID+"/restore", nil, csrf)
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("restore live account = %d, want 409; body=%q", resp.StatusCode, payload)
	}
}

// TestUX8SelfServiceOwnership 覆盖非本人 404、master 也不越权、随机 id 404。
func TestUX8SelfServiceOwnership(t *testing.T) {
	e := newAuthEnv(t)
	alice := ux8CreateUser(t, e, "ux8-alice@example.com", "correct-horse", coreauth.RoleUser)
	bob := ux8CreateUser(t, e, "ux8-bob@example.com", "correct-horse", coreauth.RoleUser)
	ux8CreateUser(t, e, "ux8-master@example.com", "correct-horse", coreauth.RoleMaster)
	aliceAccount := ux8SeedAccount(t, e, alice.ID, "13500135005")
	bobAccount := ux8SeedAccount(t, e, bob.ID, "13500135007")

	aliceClient := u1FreshClient(t)
	aliceSession := u1LoginOn(e, aliceClient, "ux8-alice@example.com", "correct-horse")
	aliceCSRF := aliceSession.CSRFToken

	// 未登录：两个自助端点都 401，且不改行。
	anonymous := u1FreshClient(t)
	for _, path := range []string{
		"/api/me/accounts/" + aliceAccount.ID + "/delete",
		"/api/me/accounts/" + aliceAccount.ID + "/restore",
	} {
		resp, payload := u1Do(e, anonymous, http.MethodPost, path, nil, "")
		if resp.StatusCode != http.StatusUnauthorized {
			t.Fatalf("anonymous %s = %d, want 401; body=%q", path, resp.StatusCode, payload)
		}
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NULL", aliceAccount.ID); got != 1 {
		t.Fatal("anonymous call changed the account")
	}

	// 他人的账号：404，不暴露存在性。
	resp, payload := u1Do(e, aliceClient, http.MethodPost,
		"/api/me/accounts/"+bobAccount.ID+"/delete", u1ReauthBody("correct-horse"), aliceCSRF)
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("delete foreign account = %d, want 404; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NULL", bobAccount.ID); got != 1 {
		t.Fatal("foreign account was deleted")
	}

	ux8SoftDelete(t, e, bobAccount.ID, bob.ID)
	resp, payload = u1Do(e, aliceClient, http.MethodPost,
		"/api/me/accounts/"+bobAccount.ID+"/restore", nil, aliceCSRF)
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("restore foreign account = %d, want 404; body=%q", resp.StatusCode, payload)
	}

	// master 的自助面也只覆盖自己的行。
	masterClient := u1FreshClient(t)
	masterSession := u1LoginOn(e, masterClient, "ux8-master@example.com", "correct-horse")
	resp, payload = u1Do(e, masterClient, http.MethodPost,
		"/api/me/accounts/"+aliceAccount.ID+"/delete", u1ReauthBody("correct-horse"), masterSession.CSRFToken)
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("master delete foreign account = %d, want 404; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NULL", aliceAccount.ID); got != 1 {
		t.Fatal("master deleted another user's account through the self endpoint")
	}

	// 不存在的 id：404。
	missing := "0192f000-0000-7000-8000-00000000dead"
	for _, path := range []string{
		"/api/me/accounts/" + missing + "/delete",
		"/api/me/accounts/" + missing + "/restore",
	} {
		resp, payload = u1Do(e, aliceClient, http.MethodPost, path, u1ReauthBody("correct-horse"), aliceCSRF)
		if resp.StatusCode != http.StatusNotFound {
			t.Fatalf("%s = %d, want 404; body=%q", path, resp.StatusCode, payload)
		}
	}
}

// TestUX8SelfRestoreGuards 覆盖管理员删除 403、超 7 天 409、已有生效账号 400。
func TestUX8SelfRestoreGuards(t *testing.T) {
	e := newAuthEnv(t)
	user := ux8CreateUser(t, e, "ux8-guard@example.com", "correct-horse", coreauth.RoleUser)
	boss := ux8CreateUser(t, e, "ux8-boss@example.com", "correct-horse", coreauth.RoleAdmin)

	client := u1FreshClient(t)
	session := u1LoginOn(e, client, "ux8-guard@example.com", "correct-horse")
	csrf := session.CSRFToken

	// 管理员删除的账号：403，行保持删除态。
	adminDeleted := ux8SeedAccount(t, e, user.ID, "13500135008")
	ux8SoftDelete(t, e, adminDeleted.ID, boss.ID)
	resp, payload := u1Do(e, client, http.MethodPost,
		"/api/me/accounts/"+adminDeleted.ID+"/restore", nil, csrf)
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("restore admin-deleted = %d, want 403; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NOT NULL", adminDeleted.ID); got != 1 {
		t.Fatal("admin-deleted account was restored")
	}

	// 超过 7 天：409。
	expired := ux8SeedAccount(t, e, user.ID, "13500135009")
	ux8BackdateDelete(t, e, expired.ID, user.ID, 8)
	resp, payload = u1Do(e, client, http.MethodPost,
		"/api/me/accounts/"+expired.ID+"/restore", nil, csrf)
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("restore expired = %d, want 409; body=%q", resp.StatusCode, payload)
	}
	if code, _ := errorCode(t, payload); code != "conflict" {
		t.Fatalf("expired restore code = %q, want conflict", code)
	}

	// 6 天前删除仍在窗口内：恢复成功（边界内一侧）。
	within := ux8SeedAccount(t, e, user.ID, "13700137000")
	ux8BackdateDelete(t, e, within.ID, user.ID, 6)
	resp, payload = u1Do(e, client, http.MethodPost,
		"/api/me/accounts/"+within.ID+"/restore", nil, csrf)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("restore within grace = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	if n := ux8AuditCount(t, e, "account.self_restore", within.ID); n != 1 {
		t.Fatalf("account.self_restore rows for within-grace = %d, want 1", n)
	}

	// 名下已有生效账号：400。
	live := ux8SeedAccount(t, e, user.ID, "13300133000")
	deleted := ux8SeedAccount(t, e, user.ID, "13400134000")
	resp, payload = u1Do(e, client, http.MethodPost,
		"/api/me/accounts/"+deleted.ID+"/delete", u1ReauthBody("correct-horse"), csrf)
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("self delete second account = %d, want 204; body=%q", resp.StatusCode, payload)
	}
	resp, payload = u1Do(e, client, http.MethodPost,
		"/api/me/accounts/"+deleted.ID+"/restore", nil, csrf)
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("restore with live account = %d, want 400; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NULL", deleted.ID); got != 0 {
		t.Fatal("account restored while another live account exists")
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NULL", live.ID); got != 1 {
		t.Fatal("live account changed")
	}
	// 三次拒绝都不写 self_restore 审计。
	for _, id := range []string{adminDeleted.ID, expired.ID, deleted.ID} {
		if n := ux8AuditCount(t, e, "account.self_restore", id); n != 0 {
			t.Fatalf("account.self_restore rows for %s = %d, want 0", id, n)
		}
	}
}

// TestUX8MailNotify 覆盖默认 true、读写、审计与未登录 401。
func TestUX8MailNotify(t *testing.T) {
	e := newAuthEnv(t)
	user := ux8CreateUser(t, e, "ux8-mail@example.com", "correct-horse", coreauth.RoleUser)

	anonymous := u1FreshClient(t)
	resp, payload := u1Do(e, anonymous, http.MethodGet, "/api/me/mail-notify", nil, "")
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("anonymous GET = %d, want 401; body=%q", resp.StatusCode, payload)
	}
	resp, payload = u1Do(e, anonymous, http.MethodPut, "/api/me/mail-notify",
		map[string]bool{"enabled": false}, "")
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("anonymous PUT = %d, want 401; body=%q", resp.StatusCode, payload)
	}

	client := u1FreshClient(t)
	session := u1LoginOn(e, client, "ux8-mail@example.com", "correct-horse")

	resp, payload = u1Do(e, client, http.MethodGet, "/api/me/mail-notify", nil, "")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("GET = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	var state ux8MailNotifyBody
	if err := json.Unmarshal(payload, &state); err != nil {
		t.Fatalf("decode GET: %v; body=%q", err, payload)
	}
	if !state.Enabled {
		t.Fatal("default mail_notify = false, want true")
	}

	resp, payload = u1Do(e, client, http.MethodPut, "/api/me/mail-notify",
		map[string]bool{"enabled": false}, session.CSRFToken)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("PUT false = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	if err := json.Unmarshal(payload, &state); err != nil || state.Enabled {
		t.Fatalf("PUT body = %+v (err %v), want enabled=false", state, err)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM users WHERE id = $1::uuid AND mail_notify = false", user.ID); got != 1 {
		t.Fatal("users.mail_notify was not written")
	}
	// 读回一致：写 false 后 GET 也是 false。
	resp, payload = u1Do(e, client, http.MethodGet, "/api/me/mail-notify", nil, "")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("GET after PUT = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	if err := json.Unmarshal(payload, &state); err != nil || state.Enabled {
		t.Fatalf("GET after PUT = %+v (err %v), want enabled=false", state, err)
	}
	detail := ux8AuditDetails(t, e, "user.mail_notify", user.ID)
	if !strings.Contains(detail, `"to":"off"`) {
		t.Fatalf("audit detail = %q, want to=off", detail)
	}
	if strings.Contains(detail, "ux8-mail@example.com") {
		t.Fatalf("audit detail carries the email: %q", detail)
	}

	resp, payload = u1Do(e, client, http.MethodPut, "/api/me/mail-notify",
		map[string]bool{"enabled": true}, session.CSRFToken)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("PUT true = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM users WHERE id = $1::uuid AND mail_notify = true", user.ID); got != 1 {
		t.Fatal("users.mail_notify was not restored to true")
	}
	if n := ux8AuditCount(t, e, "user.mail_notify", user.ID); n != 2 {
		t.Fatalf("user.mail_notify rows = %d, want 2", n)
	}
	if detail := ux8AuditDetails(t, e, "user.mail_notify", user.ID); !strings.Contains(detail, `"to":"on"`) {
		t.Fatalf("audit detail = %q, want a to=on row", detail)
	}

	// 缺 enabled 键 400。
	resp, payload = u1Do(e, client, http.MethodPut, "/api/me/mail-notify", map[string]bool{}, session.CSRFToken)
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("PUT without enabled = %d, want 400; body=%q", resp.StatusCode, payload)
	}
}

// TestUX8RoleMatrix 覆盖 master/admin/user 三角色都能操作自己的账号与开关。
func TestUX8RoleMatrix(t *testing.T) {
	e := newAuthEnv(t)
	cases := []struct {
		role  coreauth.Role
		email string
		phone string
	}{
		{coreauth.RoleMaster, "ux8-role-master@example.com", "13000000000"},
		{coreauth.RoleAdmin, "ux8-role-admin@example.com", "13100000000"},
		{coreauth.RoleUser, "ux8-role-user@example.com", "13200000044"},
	}
	for _, tc := range cases {
		t.Run(string(tc.role), func(t *testing.T) {
			owner := ux8CreateUser(t, e, tc.email, "correct-horse", tc.role)
			account := ux8SeedAccount(t, e, owner.ID, tc.phone)
			client := u1FreshClient(t)
			session := u1LoginOn(e, client, tc.email, "correct-horse")

			resp, payload := u1Do(e, client, http.MethodGet, "/api/me/mail-notify", nil, "")
			if resp.StatusCode != http.StatusOK {
				t.Fatalf("GET mail notify = %d, want 200; body=%q", resp.StatusCode, payload)
			}
			resp, payload = u1Do(e, client, http.MethodPut, "/api/me/mail-notify",
				map[string]bool{"enabled": false}, session.CSRFToken)
			if resp.StatusCode != http.StatusOK {
				t.Fatalf("PUT mail notify = %d, want 200; body=%q", resp.StatusCode, payload)
			}

			resp, payload = u1Do(e, client, http.MethodPost,
				"/api/me/accounts/"+account.ID+"/delete", u1ReauthBody("correct-horse"), session.CSRFToken)
			if resp.StatusCode != http.StatusNoContent {
				t.Fatalf("self delete = %d, want 204; body=%q", resp.StatusCode, payload)
			}
			resp, payload = u1Do(e, client, http.MethodPost,
				"/api/me/accounts/"+account.ID+"/restore", nil, session.CSRFToken)
			if resp.StatusCode != http.StatusOK {
				t.Fatalf("self restore = %d, want 200; body=%q", resp.StatusCode, payload)
			}
		})
	}
}
