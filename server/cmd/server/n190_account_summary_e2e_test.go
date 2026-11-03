package main

// N190 账号汇总 e2e：GET /api/accounts/summary 只数未软删账号的
// pending 与 rejected 行数。
//
// # 功能
// 全栈覆盖一条验收链：master 全校计数、admin 只统计绑定校、
// user 403、未认证 401、软删行不计、两种状态各计其一。
// 另钉静态段与 {id} 路由并存：字面 id "summary" 的写方法走 {id} 路由，
// 按 uuid 取用回 400。
//
// # 归属
// cmd/server 是唯一可同时引用 adapters、app、entry 的位置。
//
// # 口径
// 缺 DSN 时整包跳过并打可见横幅。跳过不是绿。
// 每个测试独占一个 schema，互不依赖。
// 手机号只取登记的合成段（tests/synthetic-fixtures.json）。
// 邮箱一律用保留域 example.com。

import (
	"encoding/json"
	"net/http"
	"testing"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

// 本文件的合成手机号全部取自登记段（tests/synthetic-fixtures.json）。
const (
	n190PhonePendingA = "13900000001"
	n190PhoneRejected = "13900000002"
	n190PhoneActive   = "13900000003"
	n190PhoneDeleted  = "13900000004"
	n190PhonePendingB = "13900000005"
)

// n190Summary 是 GET /api/accounts/summary 的响应形状。
type n190Summary struct {
	Pending  int `json:"pending"`
	Rejected int `json:"rejected"`
}

// TestN190AccountSummary 覆盖汇总端点的角色面与口径。
func TestN190AccountSummary(t *testing.T) {
	e := newAuthEnv(t)
	schoolA := u2SeedSchool(t, e, "n190-a")
	schoolB := u2SeedSchool(t, e, "n190-b")

	u2SeedUser(t, e, "n190-master@example.com", "master-pass", coreauth.RoleMaster)
	admin := u2SeedUser(t, e, "n190-admin@example.com", "admin-pass", coreauth.RoleAdmin)
	member := u2SeedUser(t, e, "n190-member@example.com", "member-pass", coreauth.RoleUser)
	u2BindSchool(t, e, admin.ID, schoolA)

	masterSession := u2LoginOn(e, e.client, "n190-master@example.com", "master-pass")
	adminClient := u2FreshClient(t)
	adminSession := u2LoginOn(e, adminClient, "n190-admin@example.com", "admin-pass")
	memberClient := u2FreshClient(t)
	memberSession := u2LoginOn(e, memberClient, "n190-member@example.com", "member-pass")

	// 校 A：pending、rejected、active、待删 pending 四行；校 B：pending 一行。
	created := map[string]string{}
	makeAccount := func(client *http.Client, session sessionUserBody, schoolID, phone string) string {
		t.Helper()
		resp, payload := u2CreateAccount(e, client, session.CSRFToken, u2CreateBody{
			SchoolID: schoolID, Phone: phone, Password: "member-pass-2", OwnerUserID: member.ID,
		})
		if resp.StatusCode != http.StatusCreated {
			t.Fatalf("create %s = %d, want 201; body=%q", phone, resp.StatusCode, payload)
		}
		var out u2Account
		if err := json.Unmarshal(payload, &out); err != nil {
			t.Fatalf("decode create %s: %v; body=%q", phone, err, payload)
		}
		return out.ID
	}
	review := func(client *http.Client, session sessionUserBody, id, decision string) {
		t.Helper()
		body := map[string]string{"decision": decision}
		if decision == "reject" {
			body["reason"] = "N190 合成驳回理由"
		}
		resp, payload := u2Do(e, client, http.MethodPost, "/api/accounts/"+id+"/review", body, session.CSRFToken)
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("review %s = %d, want 200; body=%q", decision, resp.StatusCode, payload)
		}
	}

	created["pendingA"] = makeAccount(memberClient, memberSession, schoolA, n190PhonePendingA)
	created["rejected"] = makeAccount(adminClient, adminSession, schoolA, n190PhoneRejected)
	review(adminClient, adminSession, created["rejected"], "reject")
	created["active"] = makeAccount(adminClient, adminSession, schoolA, n190PhoneActive)
	review(adminClient, adminSession, created["active"], "approve")
	created["deleted"] = makeAccount(adminClient, adminSession, schoolA, n190PhoneDeleted)
	created["pendingB"] = makeAccount(memberClient, memberSession, schoolB, n190PhonePendingB)

	// 软删一行 pending：删除后不得再进任何计数。
	delResp, delPayload := u2Do(e, adminClient, http.MethodDelete,
		"/api/accounts/"+created["deleted"], nil, adminSession.CSRFToken)
	if delResp.StatusCode != http.StatusNoContent {
		t.Fatalf("delete = %d, want 204; body=%q", delResp.StatusCode, delPayload)
	}

	fetchSummary := func(client *http.Client, csrf string) (*http.Response, []byte) {
		return u2Do(e, client, http.MethodGet, "/api/accounts/summary", nil, csrf)
	}

	// master：全校计数。校 A 剩 pending 1 + rejected 1，校 B pending 1。
	resp, payload := fetchSummary(e.client, masterSession.CSRFToken)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("master summary = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	var masterCounts n190Summary
	if err := json.Unmarshal(payload, &masterCounts); err != nil {
		t.Fatalf("decode master summary: %v; body=%q", err, payload)
	}
	if masterCounts.Pending != 2 || masterCounts.Rejected != 1 {
		t.Fatalf("master summary = %+v, want pending 2 rejected 1", masterCounts)
	}

	// admin：只统计绑定校。校 B 的 pending 与软删行都不出现。
	resp, payload = fetchSummary(adminClient, adminSession.CSRFToken)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("admin summary = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	var adminCounts n190Summary
	if err := json.Unmarshal(payload, &adminCounts); err != nil {
		t.Fatalf("decode admin summary: %v; body=%q", err, payload)
	}
	if adminCounts.Pending != 1 || adminCounts.Rejected != 1 {
		t.Fatalf("admin summary = %+v, want pending 1 rejected 1", adminCounts)
	}

	// user：403，正文是 forbidden 信封。
	resp, payload = fetchSummary(memberClient, memberSession.CSRFToken)
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("user summary = %d, want 403; body=%q", resp.StatusCode, payload)
	}
	if code, _ := errorCode(t, payload); code != "forbidden" {
		t.Fatalf("user summary code = %q, want forbidden", code)
	}

	// 未认证：401。
	anon := u2FreshClient(t)
	resp, _ = fetchSummary(anon, "")
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("anonymous summary = %d, want 401", resp.StatusCode)
	}

	// 静态段并存：字面 id "summary" 的其他方法走 {id} 路由。
	// 账号 id 在 SQL 里按 uuid 取用，非 uuid 字面量回 400 validation_failed。
	patchResp, patchPayload := u2Do(e, e.client, http.MethodPatch,
		"/api/accounts/summary", map[string]string{"name": "x"}, masterSession.CSRFToken)
	if patchResp.StatusCode != http.StatusBadRequest {
		t.Fatalf("patch /api/accounts/summary = %d, want 400; body=%q", patchResp.StatusCode, patchPayload)
	}
	if code, _ := errorCode(t, patchPayload); code != "validation_failed" {
		t.Fatalf("patch literal summary code = %q, want validation_failed", code)
	}

	// 口径锚：直接对库复核 master 计数（未删 pending=2、rejected=1）。
	if got := e.scalarInt(`SELECT count(*) FROM accounts WHERE deleted_at IS NULL AND status = 'pending'`); got != 2 {
		t.Fatalf("live pending rows = %d, want 2", got)
	}
	if got := e.scalarInt(`SELECT count(*) FROM accounts WHERE deleted_at IS NULL AND status = 'rejected'`); got != 1 {
		t.Fatalf("live rejected rows = %d, want 1", got)
	}
}
