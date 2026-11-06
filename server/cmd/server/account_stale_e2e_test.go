package main

// 账号编辑乐观锁 HTTP e2e：两个客户端先读后写，过期快照必须 409。
//
// # 功能
// 判据一：A、B 读到同一 updated_at；A 改名成功后，B 带旧值改字段 → 409。
// 判据二：过期请求不写账号、凭据与审计（含 account.update 与 account.resubmit）。
// 判据三：冲突先于字段校验与重提判定：非法手机号与会被重提的口令编辑都先报 409。
// 判据四：B 带最新 updated_at 重试 → 200，被拒账号按凭据编辑回 pending。
// 判据五：省略 expected_updated_at 保持旧行为，过期客户端仍可 last-write-wins。
//
// # 归属
// cmd/server 是唯一可同时引用 adapters、app、entry 的位置；全栈用 newAuthEnv。
//
// # 口径
// 缺 DSN 时整包跳过并打可见横幅。跳过不是绿。
// 手机号取自 tests/synthetic-fixtures.json 的登记段。

import (
	"encoding/json"
	"net/http"
	"testing"

	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

// staleAccountBody 是 GET /api/accounts/{id} 里本用例用到的字段。
// updated_at 保持字符串原样：客户端应回传服务端给的原文，不经日期类型截断。
type staleAccountBody struct {
	ID        string `json:"id"`
	Name      string `json:"name"`
	Status    string `json:"status"`
	UpdatedAt string `json:"updated_at"`
}

// staleRead 读一个账号。updated_at 必须存在，否则后续比较无意义。
func staleRead(t *testing.T, e *authEnv, client *http.Client, id string) staleAccountBody {
	t.Helper()
	resp, payload := u2Do(e, client, http.MethodGet, "/api/accounts/"+id, nil, "")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("GET account = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	var body staleAccountBody
	if err := json.Unmarshal(payload, &body); err != nil {
		t.Fatalf("decode account: %v; body=%q", err, payload)
	}
	if body.UpdatedAt == "" {
		t.Fatalf("account response has no updated_at: %s", payload)
	}
	return body
}

// staleRow 是账号行的写前快照，用来证明过期请求零写入。
type staleRow struct {
	name         string
	phone        string
	status       string
	rejectReason string
	updatedAt    string
}

func staleReadRow(t *testing.T, e *authEnv, id string) staleRow {
	t.Helper()
	return staleRow{
		name:         e.scalarString("SELECT name FROM accounts WHERE id = $1", id),
		phone:        e.scalarString("SELECT phone FROM accounts WHERE id = $1", id),
		status:       e.scalarString("SELECT status::text FROM accounts WHERE id = $1", id),
		rejectReason: e.scalarString("SELECT reject_reason FROM accounts WHERE id = $1", id),
		updatedAt:    e.scalarString("SELECT updated_at::text FROM accounts WHERE id = $1", id),
	}
}

func (r staleRow) assertUnchanged(t *testing.T, want staleRow, phase string) {
	t.Helper()
	if r != want {
		t.Fatalf("%s 改了账号行:\n got %+v\nwant %+v", phase, r, want)
	}
}

// staleAuditRows 数一次写路径会产生的审计：普通更新与重提。
func staleAuditRows(t *testing.T, e *authEnv, id string) int64 {
	t.Helper()
	return e.scalarInt(`SELECT count(*) FROM audit_logs
		WHERE target_id = $1 AND action IN ('account.update', 'account.resubmit')`, id)
}

// TestAccountUpdateExpectedUpdatedAt 覆盖并发编辑的 409 与零写。
func TestAccountUpdateExpectedUpdatedAt(t *testing.T) {
	e := newAuthEnv(t)
	const password = "owner-pass-42"
	owner := w2dSeedUser(t, e, "stale-owner@example.com", password, coreauth.RoleUser)
	account := ux8SeedAccount(t, e, owner.ID, "13800000040")
	path := "/api/accounts/" + account.ID

	// 造一个被拒账号：重提判定只有在 rejected 上才有副作用可观察。
	if _, err := e.db.Exec(e.ctx,
		`UPDATE accounts SET status = 'rejected', reject_reason = '合成驳回理由' WHERE id = $1`,
		account.ID); err != nil {
		t.Fatalf("seed rejected account: %v", err)
	}

	clientA := u1FreshClient(t)
	clientB := u1FreshClient(t)
	csrfA := u1LoginOn(e, clientA, "stale-owner@example.com", password).CSRFToken
	csrfB := u1LoginOn(e, clientB, "stale-owner@example.com", password).CSRFToken

	// 两个客户端读同一行，拿到同一个 updated_at。
	readA := staleRead(t, e, clientA, account.ID)
	readB := staleRead(t, e, clientB, account.ID)
	if readA.UpdatedAt != readB.UpdatedAt {
		t.Fatalf("两个客户端读到不同 updated_at: %q vs %q", readA.UpdatedAt, readB.UpdatedAt)
	}
	if readB.Status != "rejected" {
		t.Fatalf("status = %q, want rejected", readB.Status)
	}

	// A 先改成功：备注名编辑不改变被拒状态，但会推进 updated_at。
	resp, payload := u2Do(e, clientA, http.MethodPatch, path,
		map[string]any{"name": "A 的备注"}, csrfA)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("A 改名 = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	afterA := staleRead(t, e, clientA, account.ID)
	if afterA.UpdatedAt == readB.UpdatedAt {
		t.Fatalf("A 改名后 updated_at 未推进（%q）：乐观锁无法区分前后", afterA.UpdatedAt)
	}
	if afterA.Status != "rejected" || afterA.Name != "A 的备注" {
		t.Fatalf("A 改名后 account = %+v", afterA)
	}

	// 省略 expected_updated_at：B 仍可覆盖 A 的写入（旧客户端 last-write-wins）。
	resp, payload = u2Do(e, clientB, http.MethodPatch, path,
		map[string]any{"name": "B 的旧式编辑"}, csrfB)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("省略 expected_updated_at = %d, want 200（旧行为不变）; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarString("SELECT name FROM accounts WHERE id = $1", account.ID); got != "B 的旧式编辑" {
		t.Fatalf("省略 expected 的编辑未生效: name = %q", got)
	}

	// 从这里起 B 手里的 readB.UpdatedAt 已过期。记下写前快照。
	before := staleReadRow(t, e, account.ID)
	auditBefore := staleAuditRows(t, e, account.ID)
	credentialBefore := e.scalarInt("SELECT count(*) FROM credentials WHERE account_id = $1", account.ID)
	if credentialBefore != 0 {
		t.Fatalf("前置条件：凭据行 = %d, want 0", credentialBefore)
	}

	// 判据三之一：过期请求带非法字段仍报 409，不被 400 抢先。
	resp, payload = u2Do(e, clientB, http.MethodPatch, path, map[string]any{
		"phone":               "not-a-phone",
		"current_password":    password,
		"expected_updated_at": readB.UpdatedAt,
	}, csrfB)
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("过期 + 非法手机号 = %d, want 409; body=%q", resp.StatusCode, payload)
	}
	if code, _ := errorCode(t, payload); code != "conflict" {
		t.Fatalf("过期 + 非法手机号错误码 = %q, want conflict", code)
	}
	staleReadRow(t, e, account.ID).assertUnchanged(t, before, "过期非法字段请求")

	// 判据三之二：过期请求若放行会写凭据并重提；必须零写、无重提审计。
	resp, payload = u2Do(e, clientB, http.MethodPatch, path, map[string]any{
		"password":            "brand-new-secret",
		"current_password":    password,
		"expected_updated_at": readB.UpdatedAt,
	}, csrfB)
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("过期 + 凭据编辑 = %d, want 409; body=%q", resp.StatusCode, payload)
	}
	if code, _ := errorCode(t, payload); code != "conflict" {
		t.Fatalf("过期 + 凭据编辑错误码 = %q, want conflict", code)
	}
	if got := staleReadRow(t, e, account.ID); got != before {
		t.Fatalf("过期凭据编辑改了账号行:\n got %+v\nwant %+v", got, before)
	}
	if got := staleAuditRows(t, e, account.ID); got != auditBefore {
		t.Fatalf("过期请求产生审计: %d 条, want %d", got, auditBefore)
	}
	if got := e.scalarInt("SELECT count(*) FROM credentials WHERE account_id = $1", account.ID); got != credentialBefore {
		t.Fatalf("过期请求写了凭据: %d 行, want %d", got, credentialBefore)
	}

	// 判据四：B 刷新快照后重试成功，被拒账号按凭据编辑回 pending。
	fresh := staleRead(t, e, clientB, account.ID)
	resp, payload = u2Do(e, clientB, http.MethodPatch, path, map[string]any{
		"password":            "brand-new-secret",
		"current_password":    password,
		"expected_updated_at": fresh.UpdatedAt,
	}, csrfB)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("刷新后重试 = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarString("SELECT status::text FROM accounts WHERE id = $1", account.ID); got != string(coreaccount.StatusPending) {
		t.Fatalf("重试后 status = %q, want pending", got)
	}
	if got := e.scalarInt("SELECT count(*) FROM credentials WHERE account_id = $1", account.ID); got != 1 {
		t.Fatalf("重试后凭据行 = %d, want 1", got)
	}
	if got := e.scalarInt(`SELECT count(*) FROM audit_logs
		WHERE target_id = $1 AND action = 'account.resubmit'`, account.ID); got != 1 {
		t.Fatalf("account.resubmit 行 = %d, want 1", got)
	}
	if got := staleAuditRows(t, e, account.ID); got != auditBefore+2 {
		t.Fatalf("成功重试的审计 = %d 条, want %d", got, auditBefore+2)
	}
}
