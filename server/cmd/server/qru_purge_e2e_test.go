package main

// 物理清除端点的真 HTTP e2e：POST /api/accounts/{id}/purge 与
// POST /api/users/{id}/purge。
//
// # 功能
// 账号：master 软删后清除，行与 credentials、sign_tasks、account_egress、
// account_location 零残留；audit account.purge 恰一条且既有审计行保留；
// 活账号 409；未知 id 404；admin 可清绑定校账号、跨校 404；
// 二次口令错误零写入零审计。
// 用户：master 软删后清除，用户行、名下账号与从属行零残留；
// 被该用户审核的他人账号审核对置空；定位 actor 置空；
// 其创建的 window_overrides 保留且 created_by 置空；
// audit user.purge 恰一条；活用户 409；未知 id 404；admin 403 零审计；
// 自清 403 零写入；错口令零写入。
// 边界：清除后同手机号/同邮箱可重建（唯一键只对活行）。
//
// # 归属
// cmd/server 是唯一可同时引用 adapters、app、entry 的位置。
// 全栈复用 auth_e2e_test.go 的 newAuthEnv 与 ux2 的客户端辅助。
//
// # 口径
// 缺 DSN 时整包跳过并打可见横幅。跳过不是绿。
// 每个测试独占一个 schema，互不依赖。
// 手机号只取登记的合成段（tests/synthetic-fixtures.json）。
// 邮箱一律用保留域 example.com。

import (
	"net/http"
	"strings"
	"testing"
	"time"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

// 本文件的合成手机号全部取自登记段（tests/synthetic-fixtures.json），
// 与既有测试不共用，避免软删唯一键互相踩行。
const (
	qruPhoneGone   = "13800139000" // 账号 purge 主流程
	qruPhoneLive   = "13800139001" // 活账号 409
	qruPhoneAdmin  = "13800139002" // admin 绑定校可清 / 他人账号
	qruPhoneOther  = "13800139003" // admin 跨校 404
	qruPhoneReborn = "13800139004" // 清除后同号重建
)

// qruPassword 是全部可登录种子的口令：本文件只做门形断言，不测口令强度。
const qruPassword = "qru-purge-pass-1"

// qruNow 是软删造数用的固定时刻：测试不用墙钟（clock 门）。
var qruNow = time.Date(2026, 11, 7, 9, 0, 0, 0, time.UTC)

// qruExec 造数辅助：失败即 Fatal。
func qruExec(t *testing.T, e *authEnv, sql string, args ...any) {
	t.Helper()
	if _, err := e.db.Exec(e.ctx, sql, args...); err != nil {
		t.Fatalf("exec %q: %v", sql, err)
	}
}

// qruSeedTaskID 建一条 school_tasks 行并返回 id。
// sign_tasks 与 window_overrides 都挂在它下面；school_task 本身保留。
func qruSeedTaskID(t *testing.T, e *authEnv, schoolID, name string) string {
	t.Helper()
	qruExec(t, e, `INSERT INTO school_tasks (school_id, name) VALUES ($1::uuid, $2)`, schoolID, name)
	return e.scalarString(`SELECT id::text FROM school_tasks WHERE school_id = $1::uuid AND name = $2`,
		schoolID, name)
}

// qruSeedAccountDeps 给账号补从属行：一条任务、一份凭据、一条出口绑定与一行定位。
// updatedBy 为空串时不设定位 actor。
func qruSeedAccountDeps(t *testing.T, e *authEnv, schoolID, accountID, taskID, updatedBy string) {
	t.Helper()
	qruExec(t, e, `INSERT INTO sign_tasks (account_id, school_task_id, day, run_at)
		VALUES ($1::uuid, $2::uuid, '2026-05-01', now())`, accountID, taskID)
	qruExec(t, e, `INSERT INTO credentials (account_id, school_id, kid, nonce, ciphertext)
		VALUES ($1::uuid, $2::uuid, 'fedcba9876543210',
		        decode(repeat('ab', 12), 'hex'), decode(repeat('cd', 16), 'hex'))`, accountID, schoolID)
	qruExec(t, e, `INSERT INTO egresses (identity) VALUES ($1)`, "qru-purge-egress-"+accountID)
	egressID := e.scalarString(`SELECT id::text FROM egresses WHERE identity = $1`,
		"qru-purge-egress-"+accountID)
	qruExec(t, e, `INSERT INTO account_egress (account_id, egress_id) VALUES ($1::uuid, $2::uuid)`,
		accountID, egressID)
	if updatedBy != "" {
		qruExec(t, e, `INSERT INTO account_location (account_id, mode, lng, lat, reason, updated_by)
			VALUES ($1::uuid, 'fixed', 0.5, 0.5, 'qru seed', $2::uuid)`, accountID, updatedBy)
	} else {
		qruExec(t, e, `INSERT INTO account_location (account_id, mode, lng, lat, reason)
			VALUES ($1::uuid, 'fixed', 0.5, 0.5, 'qru seed')`, accountID)
	}
}

// qruPurge 调一次物理清除端点并返回状态码与响应体。
func qruPurge(e *authEnv, client *http.Client, csrf, path, password string) (*http.Response, []byte) {
	e.t.Helper()
	return u2Do(e, client, http.MethodPost, path,
		map[string]string{"current_password": password}, csrf)
}

// qruAuditCount 数一个对象的某动作审计行数。
func qruAuditCount(e *authEnv, action, targetType, targetID string) int64 {
	e.t.Helper()
	return e.scalarInt(`SELECT count(*) FROM audit_logs
		 WHERE action = $1 AND target_type = $2 AND target_id = $3`,
		action, targetType, targetID)
}

// qruRandomUUID 造一条随机 UUID：只保证 8-4-4-4-12 十六进制形状，
// 库侧 ::uuid 转换不校验版本位。
func qruRandomUUID(t *testing.T) string {
	t.Helper()
	h := randomHex(16)
	return h[0:8] + "-" + h[8:12] + "-" + h[12:16] + "-" + h[16:20] + "-" + h[20:32]
}

// 账号清除：master 软删后 204，行与从属行零残留，audit account.purge 恰一条；
// 活账号 409 且不写审计；未知 id 404；错口令 403 零写入零审计。
func TestQruAccountPurgeE2E(t *testing.T) {
	e := newAuthEnv(t)
	schoolID := u2SeedSchool(t, e, "qru-acct")
	master := w2dSeedUser(t, e, "qru-acct-master@example.com", qruPassword, coreauth.RoleMaster)
	owner := e.createUser("qru-acct-owner@example.com", qruPassword)
	taskID := qruSeedTaskID(t, e, schoolID, "qru acct task")

	gone := lfSeedAccount(t, e, schoolID, owner.ID, qruPhoneGone)
	qruSeedAccountDeps(t, e, schoolID, gone, taskID, master.ID)
	live := lfSeedAccount(t, e, schoolID, owner.ID, qruPhoneLive)

	client := u2FreshClient(t)
	session := u2LoginOn(e, client, master.Email, qruPassword)
	csrf := session.CSRFToken

	// master 软删（真 HTTP 删号路径）。
	resp, payload := u2Do(e, client, http.MethodDelete, "/api/accounts/"+gone,
		map[string]string{"current_password": qruPassword}, csrf)
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("soft delete = %d, want 204; body=%q", resp.StatusCode, payload)
	}

	// 错口令：403，零写入零审计（账号行仍是软删态，无 purge 审计）。
	resp, payload = qruPurge(e, client, csrf, "/api/accounts/"+gone+"/purge", "wrong-password")
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("wrong password purge = %d, want 403; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarInt(`SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NOT NULL`, gone); got != 1 {
		t.Fatalf("rejected purge must not touch the account row (soft-deleted rows = %d)", got)
	}
	if got := qruAuditCount(e, "account.purge", "account", gone); got != 0 {
		t.Fatalf("rejected purge wrote %d audit rows, want 0", got)
	}

	// 正确口令：204。
	resp, payload = qruPurge(e, client, csrf, "/api/accounts/"+gone+"/purge", qruPassword)
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("purge = %d, want 204; body=%q", resp.StatusCode, payload)
	}
	zeroAfterPurge := map[string]string{
		"accounts":         `SELECT count(*) FROM accounts WHERE id = $1::uuid`,
		"credentials":      `SELECT count(*) FROM credentials WHERE account_id = $1::uuid`,
		"sign_tasks":       `SELECT count(*) FROM sign_tasks WHERE account_id = $1::uuid`,
		"account_egress":   `SELECT count(*) FROM account_egress WHERE account_id = $1::uuid`,
		"account_location": `SELECT count(*) FROM account_location WHERE account_id = $1::uuid`,
	}
	for name, sql := range zeroAfterPurge {
		if got := e.scalarInt(sql, gone); got != 0 {
			t.Fatalf("%s rows remain after purge: %d", name, got)
		}
	}
	if got := qruAuditCount(e, "account.purge", "account", gone); got != 1 {
		t.Fatalf("account.purge audit rows = %d, want 1", got)
	}
	// 软删事实仍在审计链：append-only 表不随对象消失。
	if got := qruAuditCount(e, "account.delete", "account", gone); got != 1 {
		t.Fatalf("account.delete audit rows = %d, want 1", got)
	}

	// 活账号：409，不写审计。
	resp, payload = qruPurge(e, client, csrf, "/api/accounts/"+live+"/purge", qruPassword)
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("live purge = %d, want 409; body=%q", resp.StatusCode, payload)
	}
	if got := qruAuditCount(e, "account.purge", "account", live); got != 0 {
		t.Fatalf("conflicted purge wrote %d audit rows, want 0", got)
	}
	if got := e.scalarInt(`SELECT count(*) FROM accounts WHERE id = $1::uuid`, live); got != 1 {
		t.Fatalf("conflicted purge removed the live account")
	}

	// 未知 id：404，零审计。
	ghost := qruRandomUUID(t)
	resp, payload = qruPurge(e, client, csrf, "/api/accounts/"+ghost+"/purge", qruPassword)
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("unknown purge = %d, want 404; body=%q", resp.StatusCode, payload)
	}
	if got := qruAuditCount(e, "account.purge", "account", ghost); got != 0 {
		t.Fatalf("unknown purge wrote %d audit rows, want 0", got)
	}
	// 审计归属：account.purge 行的 school_id 记被删账号的院校。
	if got := e.scalarString(`SELECT coalesce(school_id::text, '') FROM audit_logs
		 WHERE action = 'account.purge' AND target_type = 'account' AND target_id = $1`, gone); got != schoolID {
		t.Fatalf("account.purge school_id = %q, want %q", got, schoolID)
	}
}

// 账号清除的作用域面：admin 可清绑定校账号，跨校 404（软删行原样保留）。
func TestQruAccountPurgeAdminScopeE2E(t *testing.T) {
	e := newAuthEnv(t)
	schoolA := u2SeedSchool(t, e, "qru-adm-a")
	schoolB := u2SeedSchool(t, e, "qru-adm-b")
	admin := w2dSeedUser(t, e, "qru-adm-admin@example.com", qruPassword, coreauth.RoleAdmin)
	u2BindSchool(t, e, admin.ID, schoolA)
	master := w2dSeedUser(t, e, "qru-adm-master@example.com", qruPassword, coreauth.RoleMaster)
	owner := e.createUser("qru-adm-owner@example.com", qruPassword)

	bound := lfSeedAccount(t, e, schoolA, owner.ID, qruPhoneAdmin)
	foreign := lfSeedAccount(t, e, schoolB, owner.ID, qruPhoneOther)

	// 两行都由 master 软删：造出「已软删」的前提，admin 只做清除面。
	masterClient := u2FreshClient(t)
	masterSession := u2LoginOn(e, masterClient, master.Email, qruPassword)
	for _, id := range []string{bound, foreign} {
		resp, payload := u2Do(e, masterClient, http.MethodDelete, "/api/accounts/"+id,
			map[string]string{"current_password": qruPassword}, masterSession.CSRFToken)
		if resp.StatusCode != http.StatusNoContent {
			t.Fatalf("master soft delete %s = %d, want 204; body=%q", id, resp.StatusCode, payload)
		}
	}

	client := u2FreshClient(t)
	session := u2LoginOn(e, client, admin.Email, qruPassword)

	// 绑定校：admin 清除 204。
	resp, payload := qruPurge(e, client, session.CSRFToken, "/api/accounts/"+bound+"/purge", qruPassword)
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("admin purge bound = %d, want 204; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarInt(`SELECT count(*) FROM accounts WHERE id = $1::uuid`, bound); got != 0 {
		t.Fatalf("bound account survived admin purge")
	}

	// 跨校：404，行原样保留（软删行还在，等 master 处置）。
	resp, payload = qruPurge(e, client, session.CSRFToken, "/api/accounts/"+foreign+"/purge", qruPassword)
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("admin purge cross-school = %d, want 404; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarInt(`SELECT count(*) FROM accounts WHERE id = $1::uuid`, foreign); got != 1 {
		t.Fatalf("cross-school account was removed by admin")
	}
}

// 用户清除：master 软删后 204，用户行、名下账号与从属行零残留；
// 被该用户审核的他人账号审核对置空；定位 actor 置空；
// window_overrides 保留且 created_by 置空；audit user.purge 恰一条；
// 活用户 409；admin 403 零审计；错口令零写入。
func TestQruUserPurgeE2E(t *testing.T) {
	e := newAuthEnv(t)
	schoolID := u2SeedSchool(t, e, "qru-user")
	master := w2dSeedUser(t, e, "qru-user-master@example.com", qruPassword, coreauth.RoleMaster)
	other := e.createUser("qru-user-other@example.com", qruPassword)
	taskID := qruSeedTaskID(t, e, schoolID, "qru user task")

	target := w2dSeedUser(t, e, "qru-user-target@example.com", qruPassword, coreauth.RoleUser)
	owned := lfSeedAccount(t, e, schoolID, target.ID, qruPhoneGone)
	qruSeedAccountDeps(t, e, schoolID, owned, taskID, target.ID)

	// 他人账号：target 审核过、target 更新过定位。清除后两处引用必须清空，
	// 账号行本身保留；它也是活用户 409 的靶子。
	otherAcct := lfSeedAccount(t, e, schoolID, other.ID, qruPhoneAdmin)
	qruExec(t, e, `UPDATE accounts SET reviewed_by = $2::uuid, reviewed_at = now() WHERE id = $1::uuid`,
		otherAcct, target.ID)
	qruExec(t, e, `INSERT INTO account_location (account_id, mode, lng, lat, reason, updated_by)
		VALUES ($1::uuid, 'fixed', 0.5, 0.5, 'qru seed', $2::uuid)`, otherAcct, target.ID)
	// target 建的窗口覆盖：行保留、created_by 置空。
	qruExec(t, e, `INSERT INTO window_overrides (school_task_id, day, start_at, end_at, reason, created_by)
		VALUES ($1::uuid, '2027-01-01', '2027-01-01 07:00+00', '2027-01-01 08:00+00', 'qru seed', $2::uuid)`,
		taskID, target.ID)

	client := u2FreshClient(t)
	session := u2LoginOn(e, client, master.Email, qruPassword)
	csrf := session.CSRFToken

	// 名下账号先软删（用户软删判据要求名下无活跃账号）。
	resp, payload := u2Do(e, client, http.MethodDelete, "/api/accounts/"+owned,
		map[string]string{"current_password": qruPassword}, csrf)
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("soft delete owned account = %d, want 204; body=%q", resp.StatusCode, payload)
	}
	// 软删用户。
	resp, payload = u2Do(e, client, http.MethodDelete, "/api/users/"+target.ID,
		map[string]string{"current_password": qruPassword}, csrf)
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("soft delete user = %d, want 204; body=%q", resp.StatusCode, payload)
	}

	// 错口令：403，零写入零审计（用户行仍是软删态）。
	resp, payload = qruPurge(e, client, csrf, "/api/users/"+target.ID+"/purge", "wrong-password")
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("wrong password user purge = %d, want 403; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarInt(`SELECT count(*) FROM users WHERE id = $1::uuid AND deleted_at IS NOT NULL`, target.ID); got != 1 {
		t.Fatalf("rejected purge must not touch the user row (soft-deleted rows = %d)", got)
	}
	if got := qruAuditCount(e, "user.purge", "user", target.ID); got != 0 {
		t.Fatalf("rejected purge wrote %d audit rows, want 0", got)
	}

	// 正确口令：204。
	resp, payload = qruPurge(e, client, csrf, "/api/users/"+target.ID+"/purge", qruPassword)
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("user purge = %d, want 204; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarInt(`SELECT count(*) FROM users WHERE id = $1::uuid`, target.ID); got != 0 {
		t.Fatalf("user row survived purge")
	}
	if got := e.scalarInt(`SELECT count(*) FROM accounts WHERE id = $1::uuid`, owned); got != 0 {
		t.Fatalf("owned account survived user purge")
	}
	zeroAfterPurge := map[string]string{
		"sign_tasks":       `SELECT count(*) FROM sign_tasks WHERE account_id = $1::uuid`,
		"credentials":      `SELECT count(*) FROM credentials WHERE account_id = $1::uuid`,
		"account_egress":   `SELECT count(*) FROM account_egress WHERE account_id = $1::uuid`,
		"account_location": `SELECT count(*) FROM account_location WHERE account_id = $1::uuid`,
	}
	for name, sql := range zeroAfterPurge {
		if got := e.scalarInt(sql, owned); got != 0 {
			t.Fatalf("%s rows of the purged user's account remain: %d", name, got)
		}
	}
	// 他人账号保留，审核对与定位引用成对清空。
	if got := e.scalarInt(`SELECT count(*) FROM accounts WHERE id = $1::uuid`, otherAcct); got != 1 {
		t.Fatalf("another user's account was removed by the user purge")
	}
	if got := e.scalarInt(`SELECT count(*) FROM accounts WHERE id = $1::uuid
		 AND reviewed_by IS NULL AND reviewed_at IS NULL`, otherAcct); got != 1 {
		t.Fatalf("review pair on another user's account was not cleared")
	}
	if got := e.scalarString(`SELECT coalesce(updated_by::text, '') FROM account_location WHERE account_id = $1::uuid`,
		otherAcct); got != "" {
		t.Fatalf("location actor was not cleared: %q", got)
	}
	// 窗口覆盖保留且 created_by 置空。
	if got := e.scalarInt(`SELECT count(*) FROM window_overrides
		 WHERE school_task_id = $1::uuid AND day = '2027-01-01' AND created_by IS NULL`, taskID); got != 1 {
		t.Fatalf("window override rows = %d, want 1 with created_by NULL", got)
	}
	if got := qruAuditCount(e, "user.purge", "user", target.ID); got != 1 {
		t.Fatalf("user.purge audit rows = %d, want 1", got)
	}

	// 活用户：409（other 未软删；名下还有活跃账号）。
	resp, payload = qruPurge(e, client, csrf, "/api/users/"+other.ID+"/purge", qruPassword)
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("live user purge = %d, want 409; body=%q", resp.StatusCode, payload)
	}
	if got := qruAuditCount(e, "user.purge", "user", other.ID); got != 0 {
		t.Fatalf("conflicted user purge wrote %d audit rows, want 0", got)
	}

	// 未知 id：404，零审计。
	ghost := qruRandomUUID(t)
	resp, payload = qruPurge(e, client, csrf, "/api/users/"+ghost+"/purge", qruPassword)
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("unknown user purge = %d, want 404; body=%q", resp.StatusCode, payload)
	}
	if got := qruAuditCount(e, "user.purge", "user", ghost); got != 0 {
		t.Fatalf("unknown user purge wrote %d audit rows, want 0", got)
	}

	// admin：403（master 门槛在用例层，先于存在性判定；不写审计）。
	admin := w2dSeedUser(t, e, "qru-user-admin@example.com", qruPassword, coreauth.RoleAdmin)
	u2BindSchool(t, e, admin.ID, schoolID)
	adminClient := u2FreshClient(t)
	adminSession := u2LoginOn(e, adminClient, admin.Email, qruPassword)
	resp, payload = qruPurge(e, adminClient, adminSession.CSRFToken, "/api/users/"+target.ID+"/purge", qruPassword)
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("admin user purge = %d, want 403; body=%q", resp.StatusCode, payload)
	}
	if got := qruAuditCount(e, "user.purge", "user", target.ID); got != 1 {
		t.Fatalf("admin attempt must not add audit rows (count = %d)", got)
	}
}

// 自 purge：活 master 清自己 403 forbidden，零写入零审计。
// 软删用户不可能是当前会话（软删即撤销全部会话），
// 这是用例层的防御判据，先于任何读写。
func TestQruUserPurgeSelfForbiddenE2E(t *testing.T) {
	e := newAuthEnv(t)
	master := w2dSeedUser(t, e, "qru-self-master@example.com", qruPassword, coreauth.RoleMaster)
	client := u2FreshClient(t)
	session := u2LoginOn(e, client, master.Email, qruPassword)

	resp, payload := qruPurge(e, client, session.CSRFToken, "/api/users/"+master.ID+"/purge", qruPassword)
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("self purge = %d, want 403; body=%q", resp.StatusCode, payload)
	}
	if !strings.Contains(string(payload), `"code":"forbidden"`) {
		t.Fatalf("self purge error code = %s, want forbidden", payload)
	}
	if got := e.scalarInt(`SELECT count(*) FROM users WHERE id = $1::uuid AND deleted_at IS NULL`, master.ID); got != 1 {
		t.Fatalf("self purge touched the master row (live rows = %d)", got)
	}
	if got := qruAuditCount(e, "user.purge", "user", master.ID); got != 0 {
		t.Fatalf("self purge wrote %d audit rows, want 0", got)
	}
}

// 清除后同手机号/同邮箱可重建：软删唯一键只对活行，物理删除后不再有任何挡行。
func TestQruPurgeRecreateBoundaryE2E(t *testing.T) {
	e := newAuthEnv(t)
	schoolID := u2SeedSchool(t, e, "qru-reborn")
	master := w2dSeedUser(t, e, "qru-reborn-master@example.com", qruPassword, coreauth.RoleMaster)
	owner := e.createUser("qru-reborn-owner@example.com", qruPassword)

	accountID := lfSeedAccount(t, e, schoolID, owner.ID, qruPhoneReborn)
	client := u2FreshClient(t)
	session := u2LoginOn(e, client, master.Email, qruPassword)
	csrf := session.CSRFToken

	// 活行直接清 409；先软删再清 204。
	resp, payload := qruPurge(e, client, csrf, "/api/accounts/"+accountID+"/purge", qruPassword)
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("live purge = %d, want 409; body=%q", resp.StatusCode, payload)
	}
	resp, payload = u2Do(e, client, http.MethodDelete, "/api/accounts/"+accountID,
		map[string]string{"current_password": qruPassword}, csrf)
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("soft delete = %d, want 204; body=%q", resp.StatusCode, payload)
	}
	resp, payload = qruPurge(e, client, csrf, "/api/accounts/"+accountID+"/purge", qruPassword)
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("purge = %d, want 204; body=%q", resp.StatusCode, payload)
	}
	reborn := lfSeedAccount(t, e, schoolID, owner.ID, qruPhoneReborn)
	if reborn == "" || reborn == accountID {
		t.Fatalf("recreated account id = %q, want a fresh row", reborn)
	}

	// 用户同邮箱重建：软删 → 清除 → 建同邮箱用户成功。
	user := w2dSeedUser(t, e, "qru-reborn-user@example.com", qruPassword, coreauth.RoleUser)
	if err := e.users.SoftDelete(e.ctx, user.ID, master.ID, qruNow); err != nil {
		t.Fatalf("soft delete user: %v", err)
	}
	resp, payload = qruPurge(e, client, csrf, "/api/users/"+user.ID+"/purge", qruPassword)
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("user purge = %d, want 204; body=%q", resp.StatusCode, payload)
	}
	again := w2dSeedUser(t, e, "qru-reborn-user@example.com", qruPassword, coreauth.RoleUser)
	if again.ID == "" || again.ID == user.ID {
		t.Fatalf("recreated user id = %q, want a fresh row", again.ID)
	}
}
