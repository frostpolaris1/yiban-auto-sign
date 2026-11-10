package main

// 配额写回（r4r）在真装配上的 e2e。
//
// 夹具（newAuthEnv）默认把两键抬到 10000/10000，服务既有 e2e 的多用户场景；
// 本文件删掉抬额行，验证生产缺省 1/1、写回热生效与审计/回滚：
//  1. 缺省 1/1：第 1 个用户（master）恰好过，第 2 个用户注册 429 capacity。
//  2. PUT 抬到 5 后第 2–5 用户过、第 6 拒；null 删行回 1：存量不动只拒新写。
//  3. max_accounts=0：连续建号不因全局配额拒；同一属主第二号仍 409 conflict。
//  4. 审计恰一行含 changes 旧→新；审计失败整体回滚（覆盖行不落地）。
//  5. GET 回显 source=default → db。

import (
	"context"
	"encoding/json"
	"net/http"
	"strconv"
	"testing"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

// seedQuota 把两枚配额键写成覆盖行（store 注入，与写面同源）。
func seedQuota(t *testing.T, ctx context.Context, db *pg.DB, users, accounts int) {
	t.Helper()
	for key, value := range map[string]int{
		"quota.max_users":    users,
		"quota.max_accounts": accounts,
	} {
		if _, err := db.Exec(ctx, `
			INSERT INTO settings (key, value) VALUES ($1, $2)
			ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value`,
			key, strconv.Itoa(value)); err != nil {
			t.Fatalf("seed quota %s: %v", key, err)
		}
	}
}

// clearQuota 删掉两枚配额覆盖行，回到生产缺省 1/1。
func clearQuota(t *testing.T, ctx context.Context, db *pg.DB) {
	t.Helper()
	if _, err := db.Exec(ctx,
		`DELETE FROM settings WHERE key IN ('quota.max_users', 'quota.max_accounts')`); err != nil {
		t.Fatalf("clear quota rows: %v", err)
	}
}

// quotaReport 是 GET/PUT 响应的解析形状。
type quotaReport struct {
	MaxUsers struct {
		Value  int    `json:"value"`
		Source string `json:"source"`
	} `json:"max_users"`
	MaxAccounts struct {
		Value  int    `json:"value"`
		Source string `json:"source"`
	} `json:"max_accounts"`
}

// putQuota 以 master 会话发一次 PUT /api/admin/quota-settings。
func putQuota(t *testing.T, e *authEnv, csrf, body string) (*http.Response, []byte, quotaReport) {
	t.Helper()
	resp, payload := e.do(http.MethodPut, "/api/admin/quota-settings",
		json.RawMessage(body), map[string]string{"X-CSRF-Token": csrf})
	var report quotaReport
	if resp.StatusCode == http.StatusOK {
		if err := json.Unmarshal(payload, &report); err != nil {
			t.Fatalf("decode PUT quota report: %v; body=%q", err, payload)
		}
	}
	return resp, payload, report
}

// 缺省 1/1 经真装配生效；PUT 抬额后同一注册路径立即放行；null 删行回 1。
func TestQuotaDefaultsAndWritebackThroughAssembly(t *testing.T) {
	e := newAuthEnv(t)
	clearQuota(t, e.ctx, e.db)

	// 第 1 个用户：s05Master 直写仓储（等价 create-master 的种子位），
	// 正好占满缺省的 1 个名额。
	csrf := s05Master(t, e)
	s05SetRegistration(t, e, csrf, true)

	// GET 回显缺省 1/1。
	resp, payload := e.do(http.MethodGet, "/api/admin/quota-settings", nil, nil)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("GET quota-settings = %d; body=%q", resp.StatusCode, payload)
	}
	var report quotaReport
	if err := json.Unmarshal(payload, &report); err != nil {
		t.Fatalf("decode quota report: %v; body=%q", err, payload)
	}
	if report.MaxUsers.Value != 1 || report.MaxUsers.Source != "default" {
		t.Fatalf("default max_users = %+v, want 1/default", report.MaxUsers)
	}

	// 第 2 个用户注册：缺省 1 已满 → 429 capacity_exceeded。
	register := func(email string) (*http.Response, []byte) {
		return s05Do(t, e, s05Client(t), http.MethodPost, "/api/auth/register",
			s05RegisterBody(email, s05Password), nil)
	}
	resp, payload = register("quota-over-default@example.com")
	if resp.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("register at default quota = %d, want 429; body=%q", resp.StatusCode, payload)
	}
	if code, _ := errorCode(t, payload); code != "capacity_exceeded" {
		t.Fatalf("register code = %q, want capacity_exceeded", code)
	}

	// PUT 抬到 5：写回热生效，第 2–5 用户过、第 6 拒。
	resp, payload, report = putQuota(t, e, csrf, `{"max_users":5}`)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("PUT quota-settings = %d; body=%q", resp.StatusCode, payload)
	}
	if report.MaxUsers.Value != 5 || report.MaxUsers.Source != "db" {
		t.Fatalf("PUT max_users = %+v, want 5/db", report.MaxUsers)
	}
	for i := 2; i <= 5; i++ {
		resp, payload = register("quota-user-" + strconv.Itoa(i) + "@example.com")
		if resp.StatusCode != http.StatusCreated {
			t.Fatalf("register user %d after raise = %d, want 201; body=%q", i, resp.StatusCode, payload)
		}
	}
	resp, payload = register("quota-user-6@example.com")
	if resp.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("6th register at max_users=5 = %d, want 429; body=%q", resp.StatusCode, payload)
	}

	// 审计恰一行，detail 记旧→新（旧为 nil：缺行）。
	if n := e.scalarInt(
		"SELECT count(*) FROM audit_logs WHERE action = 'settings.quota_writeback'"); n != 1 {
		t.Fatalf("quota_writeback rows = %d, want 1", n)
	}
	detail := e.scalarString(
		"SELECT detail FROM audit_logs WHERE action = 'settings.quota_writeback' ORDER BY id DESC LIMIT 1")
	if detail != `{"changes":{"quota.max_users":[null,5]}}` {
		t.Fatalf("audit detail = %s", detail)
	}

	// null 删行 → 回缺省 1；5 个存量用户不动，新注册被拒。
	resp, payload, report = putQuota(t, e, csrf, `{"max_users":null}`)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("PUT null = %d; body=%q", resp.StatusCode, payload)
	}
	if report.MaxUsers.Value != 1 || report.MaxUsers.Source != "default" {
		t.Fatalf("PUT null max_users = %+v, want 1/default", report.MaxUsers)
	}
	if n := e.scalarInt("SELECT count(*) FROM users WHERE deleted_at IS NULL"); n != 5 {
		t.Fatalf("live users after clear = %d, want 5 (existing rows must stay)", n)
	}
	resp, payload = register("quota-after-clear@example.com")
	if resp.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("register after clear = %d, want 429; body=%q", resp.StatusCode, payload)
	}
	if n := e.scalarInt(
		"SELECT count(*) FROM audit_logs WHERE action = 'settings.quota_writeback'"); n != 2 {
		t.Fatalf("quota_writeback rows after clear = %d, want 2", n)
	}
}

// max_accounts=0：连续建号不因全局配额拒；同一属主第二号仍 conflict。
func TestQuotaAccountLineThroughAssembly(t *testing.T) {
	e := newAuthEnv(t)
	clearQuota(t, e.ctx, e.db)

	master := u2SeedUser(t, e, "quota-account-master@example.com", "master-pass", coreauth.RoleMaster)
	ownerA := u2SeedUser(t, e, "quota-account-a@example.com", "member-pass", coreauth.RoleUser)
	ownerB := u2SeedUser(t, e, "quota-account-b@example.com", "member-pass", coreauth.RoleUser)
	ownerC := u2SeedUser(t, e, "quota-account-c@example.com", "member-pass", coreauth.RoleUser)
	csrf := u2LoginOn(e, e.client, master.Email, "master-pass").CSRFToken
	school := u2SeedSchool(t, e, "quota-unlimited")

	if resp, payload, report := putQuota(t, e, csrf, `{"max_accounts":0}`); resp.StatusCode != http.StatusOK {
		t.Fatalf("PUT max_accounts=0 = %d; body=%q", resp.StatusCode, payload)
	} else if report.MaxAccounts.Value != 0 || report.MaxAccounts.Source != "db" {
		t.Fatalf("PUT max_accounts = %+v, want db 0", report.MaxAccounts)
	}

	create := func(ownerID, phone string) (*http.Response, []byte) {
		return u2Do(e, e.client, http.MethodPost, "/api/accounts", u2CreateBody{
			SchoolID: school, Phone: phone, Password: "secret", OwnerUserID: ownerID,
		}, csrf)
	}
	for i, owner := range []string{master.ID, ownerA.ID, ownerB.ID} {
		phone := "1350001000" + strconv.Itoa(i+1)
		if resp, payload := create(owner, phone); resp.StatusCode != http.StatusCreated {
			t.Fatalf("account %d under unlimited = %d, want 201; body=%q", i, resp.StatusCode, payload)
		}
	}
	// 一人一号判据独立：同一属主第二号仍 conflict。
	resp, payload := create(master.ID, "13500010004")
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("same-owner second account = %d, want 409; body=%q", resp.StatusCode, payload)
	}

	// 收紧到 1：已有 3 行不动，新属主建号 429 capacity。
	if resp, payload, _ := putQuota(t, e, csrf, `{"max_accounts":1}`); resp.StatusCode != http.StatusOK {
		t.Fatalf("PUT max_accounts=1 = %d; body=%q", resp.StatusCode, payload)
	}
	resp, payload = create(ownerC.ID, "13500010005")
	if resp.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("account over max_accounts=1 = %d, want 429; body=%q", resp.StatusCode, payload)
	}
	if n := e.scalarInt("SELECT count(*) FROM accounts WHERE deleted_at IS NULL"); n != 3 {
		t.Fatalf("live accounts = %d, want 3 (existing rows must stay)", n)
	}
}

// 审计失败整体回滚：覆盖行不落地，PUT 回 500。
func TestQuotaAuditFailureRollsBackThroughAssembly(t *testing.T) {
	e := newAuthEnv(t)
	clearQuota(t, e.ctx, e.db)
	csrf := s05Master(t, e)

	unblock := blockAuditInserts(t, e)
	resp, payload, _ := putQuota(t, e, csrf, `{"max_users":5}`)
	unblock()
	if resp.StatusCode != http.StatusInternalServerError {
		t.Fatalf("PUT with blocked audit = %d, want 500; body=%q", resp.StatusCode, payload)
	}
	if n := e.scalarInt("SELECT count(*) FROM settings WHERE key = 'quota.max_users'"); n != 0 {
		t.Fatalf("override row survived the audit failure: %d rows", n)
	}
}

// blockAuditInserts 让 audit_logs 的 INSERT 立即失败（与 pg e2e 同形）。
func blockAuditInserts(t *testing.T, e *authEnv) func() {
	t.Helper()
	fn := e.schema + ".quota_block_audit"
	statements := []string{
		"DROP TRIGGER IF EXISTS quota_block_audit ON audit_logs",
		"DROP FUNCTION IF EXISTS " + fn + "()",
		"CREATE FUNCTION " + fn + "() RETURNS trigger LANGUAGE plpgsql AS $body$ " +
			"BEGIN RAISE EXCEPTION 'audit writes are blocked by the test harness'; END $body$",
		"CREATE TRIGGER quota_block_audit BEFORE INSERT ON audit_logs " +
			"FOR EACH ROW EXECUTE FUNCTION " + fn + "()",
	}
	for _, statement := range statements {
		if _, err := e.db.Exec(e.ctx, statement); err != nil {
			t.Fatalf("prepare the audit failure trigger: %v", err)
		}
	}
	return func() {
		if _, err := e.db.Exec(e.ctx, "DROP TRIGGER IF EXISTS quota_block_audit ON audit_logs"); err != nil {
			t.Errorf("drop the audit failure trigger: %v", err)
		}
	}
}
