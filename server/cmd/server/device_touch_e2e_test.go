package main

// 设备刷新的乐观锁 e2e：写回凭据必须同步推进账号 updated_at。
//
// # 功能
// 判据一：active 账号的设备字段真正变化后，updated_at 前进。
// 判据二：平台回读同值不推进 updated_at，也不写凭据（审计仍照常追加）。
// 判据三：刷新前读到的 expected_updated_at 改 device_code/phone_model → 409，
// 凭据版本、解密后的明文与审计都不变。
// 判据四：带刷新后的 expected_updated_at 重试 → 200，凭据与审计各前进一步。
// 判据五：Touch 失败（测试 schema 注入 BEFORE UPDATE 异常）→ 500，
// 本次凭据写回与审计整体回滚；移除注入后同一刷新成功。
//
// # 归属
// cmd/server 是唯一可同时引用 adapters、app、entry 的位置；
// 设备读回复用 ux2 的可编程 reader。
//
// # 口径
// 缺 DSN 时整包跳过并打可见横幅。跳过不是绿。
// 每个测试独占一个 schema，互不依赖。
// 手机号只取登记的合成段（tests/synthetic-fixtures.json）。

import (
	"encoding/json"
	"net/http"
	"testing"
	"time"

	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

// u2DeviceTimestamp 读账号详情里的 updated_at。
func u2DeviceTimestamp(t *testing.T, e *u2Env, accountID string, issued *coreauth.Issued) time.Time {
	t.Helper()
	code, payload := e.do(http.MethodGet, "/api/accounts/"+accountID, nil, issued)
	if code != http.StatusOK {
		t.Fatalf("GET account = %d, want 200; body=%q", code, payload)
	}
	var body struct {
		UpdatedAt time.Time `json:"updated_at"`
	}
	if err := json.Unmarshal(payload, &body); err != nil {
		t.Fatalf("decode account: %v; body=%q", err, payload)
	}
	if body.UpdatedAt.IsZero() {
		t.Fatalf("account response has no updated_at: %s", payload)
	}
	return body.UpdatedAt
}

// u2DeviceSeedActive 造一个带初始设备码与机型的 active 账号，返回 ID 与 master 会话。
func u2DeviceSeedActive(t *testing.T, e *u2Env, label string) (string, *coreauth.Issued) {
	t.Helper()
	schoolID := e.seedSchool(label)
	master := e.seedUser("u2-"+label+"-master@example.com", coreauth.RoleMaster)
	owner := e.seedUser("u2-"+label+"-owner@example.com", coreauth.RoleUser)
	accountID := e.seedAccount(schoolID, owner.ID, u2DevicePhoneOwn, "SEEDDEV01", "SeedPhone 1")
	if _, err := e.db.Exec(e.ctx, `UPDATE accounts SET status = 'active' WHERE id = $1`, accountID); err != nil {
		t.Fatalf("activate account: %v", err)
	}
	return accountID, e.issue(master)
}

// u2DeviceRefresh 发一次设备刷新，要求 200。
func u2DeviceRefresh(t *testing.T, e *u2Env, path string, issued *coreauth.Issued) {
	t.Helper()
	code, payload := e.do(http.MethodPost, path+"/device-refresh",
		u1ReauthBody(w2cSeedPassword), issued)
	if code != http.StatusOK {
		t.Fatalf("device refresh = %d, want 200; body=%q", code, payload)
	}
}

// TestUX2G13DeviceRefreshAdvancesUpdatedAt 覆盖刷新推进时间戳与旧快照 409。
func TestUX2G13DeviceRefreshAdvancesUpdatedAt(t *testing.T) {
	e := u2NewEnv(t)
	accountID, issued := u2DeviceSeedActive(t, e, "touch")
	path := "/api/accounts/" + accountID

	// 判据一：设备字段变化 → updated_at 前进。
	beforeRefresh := u2DeviceTimestamp(t, e, accountID, issued)
	e.reader.script = []u2DeviceOutcome{{
		state: appaccount.DeviceState{Authorized: true, Code: "TOUCHCODE-0001", PhoneModel: "Touch Phone 1"},
	}}
	u2DeviceRefresh(t, e, path, issued)
	afterRefresh := u2DeviceTimestamp(t, e, accountID, issued)
	if !afterRefresh.After(beforeRefresh) {
		t.Fatalf("设备刷新未推进 updated_at: %s → %s", beforeRefresh, afterRefresh)
	}

	// 判据二：平台回读同值 → 时间戳与凭据版本都不动，审计仍一条。
	e.reader.script = []u2DeviceOutcome{{
		state: appaccount.DeviceState{Authorized: true, Code: "TOUCHCODE-0001", PhoneModel: "Touch Phone 1"},
	}}
	versionBefore := e.credentialVersion(accountID)
	refreshAudits := e.auditCount(accountID, "account.device_refresh")
	u2DeviceRefresh(t, e, path, issued)
	stable := u2DeviceTimestamp(t, e, accountID, issued)
	if !stable.Equal(afterRefresh) {
		t.Fatalf("无变化刷新推进了 updated_at: %s → %s", afterRefresh, stable)
	}
	if got := e.credentialVersion(accountID); got != versionBefore {
		t.Fatalf("无变化刷新的凭据版本 = %d, want %d", got, versionBefore)
	}
	if got := e.auditCount(accountID, "account.device_refresh"); got != refreshAudits+1 {
		t.Fatalf("无变化刷新的审计 = %d 条, want %d", got, refreshAudits+1)
	}

	// 判据三：带刷新前的 expected 改设备 → 409，凭据与审计都不动。
	credentialBefore := e.credentialVersion(accountID)
	payloadBefore := e.credentialPayload(accountID)
	auditsBefore := e.auditCount(accountID, "account.device_refresh") +
		e.auditCount(accountID, "account.update")
	code, payload := e.do(http.MethodPatch, path, map[string]any{
		"device_code":         "STALE-DEVICE",
		"phone_model":         "Stale Phone",
		"current_password":    w2cSeedPassword,
		"expected_updated_at": beforeRefresh,
	}, issued)
	if code != http.StatusConflict {
		t.Fatalf("过期设备编辑 = %d, want 409; body=%q", code, payload)
	}
	if errCode, _ := errorCode(t, payload); errCode != "conflict" {
		t.Fatalf("过期设备编辑错误码 = %q, want conflict", errCode)
	}
	if got := e.credentialVersion(accountID); got != credentialBefore {
		t.Fatalf("过期设备编辑改了凭据版本: %d, want %d", got, credentialBefore)
	}
	if got := e.credentialPayload(accountID); got != payloadBefore {
		t.Fatalf("过期设备编辑改了凭据明文:\n got %+v\nwant %+v", got, payloadBefore)
	}
	if got := e.auditCount(accountID, "account.device_refresh") +
		e.auditCount(accountID, "account.update"); got != auditsBefore {
		t.Fatalf("过期设备编辑产生审计: %d 条, want %d", got, auditsBefore)
	}

	// 判据四：带刷新后的 expected 重试 → 200，凭据与审计各前进一步。
	current := u2DeviceTimestamp(t, e, accountID, issued)
	if !current.Equal(stable) {
		t.Fatalf("过期请求后 updated_at 变了: %s → %s", stable, current)
	}
	code, payload = e.do(http.MethodPatch, path, map[string]any{
		"device_code":         "FRESH-DEVICE",
		"phone_model":         "Fresh Phone",
		"current_password":    w2cSeedPassword,
		"expected_updated_at": current,
	}, issued)
	if code != http.StatusOK {
		t.Fatalf("刷新后设备编辑 = %d, want 200; body=%q", code, payload)
	}
	if got := e.credentialPayload(accountID); got.DeviceCode != "FRESH-DEVICE" || got.PhoneModel != "Fresh Phone" {
		t.Fatalf("刷新后设备编辑未落库: %+v", got)
	}
	if got := u2DeviceTimestamp(t, e, accountID, issued); !got.After(current) {
		t.Fatalf("成功的设备编辑未推进 updated_at: %s → %s", current, got)
	}
	if got := e.auditCount(accountID, "account.update"); got != 1 {
		t.Fatalf("成功的设备编辑审计 = %d 条, want 1", got)
	}
}

// TestUX2G13DeviceRefreshTouchFailureRollsBack 钉住 Touch 失败时的整体回滚。
//
// 测试 schema 注入 BEFORE UPDATE 异常，让事务内唯一的 accounts 写（Touch）失败；
// 若凭据写回或审计在 Touch 之前已提交，本用例红。
func TestUX2G13DeviceRefreshTouchFailureRollsBack(t *testing.T) {
	e := u2NewEnv(t)
	accountID, issued := u2DeviceSeedActive(t, e, "touch-rollback")
	path := "/api/accounts/" + accountID

	versionBefore := e.credentialVersion(accountID)
	payloadBefore := e.credentialPayload(accountID)
	auditsBefore := e.auditCount(accountID, "account.device_refresh")

	if _, err := e.db.Exec(e.ctx, `
		CREATE FUNCTION ux2_reject_account_update() RETURNS trigger
		LANGUAGE plpgsql AS $$
		BEGIN
			RAISE EXCEPTION 'account update blocked';
		END
		$$`); err != nil {
		t.Fatalf("create failing function: %v", err)
	}
	if _, err := e.db.Exec(e.ctx, `
		CREATE TRIGGER ux2_reject_account_update BEFORE UPDATE ON accounts
		FOR EACH ROW EXECUTE FUNCTION ux2_reject_account_update()`); err != nil {
		t.Fatalf("create failing trigger: %v", err)
	}
	t.Cleanup(func() {
		_, _ = e.db.Exec(e.ctx, "DROP TRIGGER IF EXISTS ux2_reject_account_update ON accounts")
		_, _ = e.db.Exec(e.ctx, "DROP FUNCTION IF EXISTS ux2_reject_account_update()")
	})

	e.reader.script = []u2DeviceOutcome{{
		state: appaccount.DeviceState{Authorized: true, Code: "ROLLBACK-CODE-1", PhoneModel: "Rollback Phone"},
	}}
	code, payload := e.do(http.MethodPost, path+"/device-refresh",
		u1ReauthBody(w2cSeedPassword), issued)
	if code != http.StatusInternalServerError {
		t.Fatalf("Touch 失败的刷新 = %d, want 500; body=%q", code, payload)
	}
	if errCode, _ := errorCode(t, payload); errCode != "internal_error" {
		t.Fatalf("Touch 失败的刷新错误码 = %q, want internal_error", errCode)
	}
	if got := e.credentialVersion(accountID); got != versionBefore {
		t.Fatalf("Touch 失败后凭据版本 = %d, want %d（凭据未回滚）", got, versionBefore)
	}
	if got := e.credentialPayload(accountID); got != payloadBefore {
		t.Fatalf("Touch 失败后凭据明文变了:\n got %+v\nwant %+v", got, payloadBefore)
	}
	if got := e.auditCount(accountID, "account.device_refresh"); got != auditsBefore {
		t.Fatalf("Touch 失败后审计 = %d 条, want %d（审计未回滚）", got, auditsBefore)
	}

	// 移除注入后同一刷新成功：证明上面的失败确由 Touch 的 UPDATE 引起。
	if _, err := e.db.Exec(e.ctx, "DROP TRIGGER ux2_reject_account_update ON accounts"); err != nil {
		t.Fatalf("drop failing trigger: %v", err)
	}
	if _, err := e.db.Exec(e.ctx, "DROP FUNCTION ux2_reject_account_update()"); err != nil {
		t.Fatalf("drop failing function: %v", err)
	}
	u2DeviceRefresh(t, e, path, issued)
	if got := e.credentialVersion(accountID); got != versionBefore+1 {
		t.Fatalf("恢复后的凭据版本 = %d, want %d", got, versionBefore+1)
	}
	if got := e.auditCount(accountID, "account.device_refresh"); got != auditsBefore+1 {
		t.Fatalf("恢复后的审计 = %d 条, want %d", got, auditsBefore+1)
	}
}
