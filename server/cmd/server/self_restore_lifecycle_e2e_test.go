package main

// 自助恢复（/api/me/restore）在账号生命周期边界上的端到端测试。
//
// # 功能
// 覆盖两条只有真装配才看得见的路径：
//  1. 属主自删后名下另有 live 账号：一人一号优先，用户恢复 204 但账号不复活，
//     审计 Detail.account_restored=false。
//  2. 审计写失败：恢复事务整体回滚（用户与账号仍软删、零审计行），
//     修复审计表后同一请求 204，Detail.account_restored=true。
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
	"encoding/json"
	"net/http"
	"testing"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

// selfRestoreAuditDetail 读取某 target 最近一条审计的 detail 对象。
func selfRestoreAuditDetail(t *testing.T, e *authEnv, action, targetID string) map[string]any {
	t.Helper()
	raw := e.scalarString(
		"SELECT detail FROM audit_logs WHERE action = $1 AND target_id = $2 ORDER BY id DESC LIMIT 1",
		action, targetID)
	var detail map[string]any
	if err := json.Unmarshal([]byte(raw), &detail); err != nil {
		t.Fatalf("decode audit detail %q: %v", raw, err)
	}
	return detail
}

// TestSelfRestoreWithLiveSibling 钉住「用户恢复成功、账号因一人一号不复活」。
//
// 夹具走真实自助注销：用户与名下账号同一时间戳软删；随后直接造一行新的
// live 账号，代表属主名下已经有生效账号。恢复时用户行照常复活，旧账号
// 留在删除态，审计必须记 account_restored=false。
func TestSelfRestoreWithLiveSibling(t *testing.T) {
	e := newAuthEnv(t)
	// quota.max_accounts=10：容量远未到顶，账号不复活只能是一人一号挡的。
	seedQuota(t, e.ctx, e.db, 10000, 10)

	user := ux8CreateUser(t, e, "self-sibling@example.com", "correct-horse", coreauth.RoleUser)
	old := ux8SeedAccount(t, e, user.ID, "13500135011")

	client := u1FreshClient(t)
	session := u1LoginOn(e, client, "self-sibling@example.com", "correct-horse")
	resp, payload := u1Do(e, client, http.MethodPost, meDeletePath,
		u1ReauthBody("correct-horse"), session.CSRFToken)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("self delete = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NOT NULL", old.ID); got != 1 {
		t.Fatal("self delete did not soft-delete the account")
	}

	// 真实注销会连带删掉全部 live 账号；这里另造一行 live 账号，
	// 把「属主已有活号」这一恢复守卫条件摆出来。
	sibling := ux8SeedAccount(t, e, user.ID, "13500135012")
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NULL", sibling.ID); got != 1 {
		t.Fatal("sibling is not live before restore")
	}

	resp, payload = u1Do(e, u1FreshClient(t), http.MethodPost, meRestorePath,
		map[string]string{"email": "self-sibling@example.com", "password": "correct-horse"}, "")
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("self restore = %d, want 204; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM users WHERE id = $1::uuid AND deleted_at IS NULL", user.ID); got != 1 {
		t.Fatal("user is not live after restore")
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NOT NULL", old.ID); got != 1 {
		t.Fatal("account was restored despite a live sibling (one-account rule broken)")
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NULL", sibling.ID); got != 1 {
		t.Fatal("live sibling changed during restore")
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE owner_user_id = $1::uuid AND deleted_at IS NULL", user.ID); got != 1 {
		t.Fatalf("owner live accounts = %d, want 1", got)
	}
	if n := ux8AuditCount(t, e, "user.self_restore", user.ID); n != 1 {
		t.Fatalf("user.self_restore rows = %d, want 1", n)
	}
	detail := selfRestoreAuditDetail(t, e, "user.self_restore", user.ID)
	if got, ok := detail["account_restored"]; !ok || got != false {
		t.Fatalf("audit account_restored = %#v (present=%v), want false", got, ok)
	}
	if got := detail["field"]; got != "deleted_at" {
		t.Fatalf("audit field = %#v, want deleted_at", got)
	}
}

// TestSelfRestoreAuditFailureRollsBack 钉住审计失败即事务整体回滚。
//
// 先把 audit_logs 改名制造写失败：恢复请求 500，用户与账号仍是软删行；
// 改回表名后同一请求 204，账号一并复活，审计记 account_restored=true。
func TestSelfRestoreAuditFailureRollsBack(t *testing.T) {
	e := newAuthEnv(t)

	user := ux8CreateUser(t, e, "self-audit-down@example.com", "correct-horse", coreauth.RoleUser)
	account := ux8SeedAccount(t, e, user.ID, "13500135013")

	client := u1FreshClient(t)
	session := u1LoginOn(e, client, "self-audit-down@example.com", "correct-horse")
	resp, payload := u1Do(e, client, http.MethodPost, meDeletePath,
		u1ReauthBody("correct-horse"), session.CSRFToken)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("self delete = %d, want 200; body=%q", resp.StatusCode, payload)
	}

	if _, err := e.db.Exec(e.ctx, "ALTER TABLE audit_logs RENAME TO audit_logs_unavailable"); err != nil {
		t.Fatalf("make audit write fail: %v", err)
	}
	resp, payload = u1Do(e, u1FreshClient(t), http.MethodPost, meRestorePath,
		map[string]string{"email": "self-audit-down@example.com", "password": "correct-horse"}, "")
	if _, err := e.db.Exec(e.ctx, "ALTER TABLE audit_logs_unavailable RENAME TO audit_logs"); err != nil {
		t.Fatalf("restore audit table name: %v", err)
	}
	if resp.StatusCode != http.StatusInternalServerError {
		t.Fatalf("restore with audit write failure = %d, want 500; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM users WHERE id = $1::uuid AND deleted_at IS NOT NULL", user.ID); got != 1 {
		t.Fatal("audit failure committed the user restore; want rollback")
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NOT NULL", account.ID); got != 1 {
		t.Fatal("audit failure committed the account restore; want rollback")
	}
	if n := ux8AuditCount(t, e, "user.self_restore", user.ID); n != 0 {
		t.Fatalf("user.self_restore rows after audit failure = %d, want 0", n)
	}

	resp, payload = u1Do(e, u1FreshClient(t), http.MethodPost, meRestorePath,
		map[string]string{"email": "self-audit-down@example.com", "password": "correct-horse"}, "")
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("self restore after audit repair = %d, want 204; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM users WHERE id = $1::uuid AND deleted_at IS NULL", user.ID); got != 1 {
		t.Fatal("user is not live after repaired restore")
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NULL", account.ID); got != 1 {
		t.Fatal("account is not live after repaired restore")
	}
	if n := ux8AuditCount(t, e, "user.self_restore", user.ID); n != 1 {
		t.Fatalf("user.self_restore rows = %d, want 1", n)
	}
	detail := selfRestoreAuditDetail(t, e, "user.self_restore", user.ID)
	if got, ok := detail["account_restored"]; !ok || got != true {
		t.Fatalf("audit account_restored = %#v (present=%v), want true", got, ok)
	}
}
