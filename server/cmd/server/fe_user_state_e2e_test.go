package main

// 首页/诊断读面的账号身份字段（真库、真 HTTP）。
//
// # 功能
// /api/me/sign-status、/api/me/diagnostics 与
// /api/accounts/{id}/diagnostics 下发 display_name、phone_masked、
// account_status；无任务行的待审账号仍在轮询列表里。
//
// # 口径
// 展示名取账号仓储已派生的有效备注名（备注名或「账号+尾 4 位」），
// 手机号只出掩码。跨用户与跨校读取仍 404；响应不含完整手机号。

import (
	"net/http"
	"strings"
	"testing"

	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

func TestMeStateFieldsShowAccountIdentity(t *testing.T) {
	e := w2cNewEnv(t)
	owner := e.user("fe-state-owner@example.com", coreauth.RoleUser)
	issued := e.login(owner)
	school := e.school("fe-state-school")

	activeID := e.account(school.ID, owner.ID, "13800061234")
	e.activateAccount(activeID)
	if _, err := e.db.Exec(e.ctx,
		"UPDATE accounts SET name = $2 WHERE id = $1::uuid", activeID, "小明"); err != nil {
		t.Fatalf("set remark name: %v", err)
	}
	// 待审账号：默认 pending，当日没有任务行。
	pendingID := e.account(school.ID, owner.ID, "13800065678")

	type readItem struct {
		AccountID     string  `json:"account_id"`
		DisplayName   string  `json:"display_name"`
		PhoneMasked   string  `json:"phone_masked"`
		AccountStatus string  `json:"account_status"`
		State         *string `json:"state"`
	}
	type readReport struct {
		Items []readItem `json:"items"`
	}

	// 首页轮询：两个账号都在列表里，字段各自正确。
	rec := e.do(http.MethodGet, "/api/me/sign-status", nil, issued)
	if rec.Code != http.StatusOK {
		t.Fatalf("sign-status status = %d: %s", rec.Code, rec.Body.String())
	}
	var status readReport
	e.decode(rec, &status)
	statusBody := rec.Body.String()
	statusByID := map[string]readItem{}
	for _, it := range status.Items {
		statusByID[it.AccountID] = it
	}
	if len(status.Items) != 2 {
		t.Fatalf("sign-status items = %d, want 2", len(status.Items))
	}
	if got := statusByID[activeID]; got.DisplayName != "小明" ||
		got.PhoneMasked != "138****1234" || got.AccountStatus != string(coreaccount.StatusActive) {
		t.Fatalf("sign-status active = %+v, want 小明/138****1234/active", got)
	}
	if got := statusByID[pendingID]; got.DisplayName != "账号5678" ||
		got.PhoneMasked != "138****5678" || got.AccountStatus != string(coreaccount.StatusPending) ||
		got.State != nil {
		t.Fatalf("sign-status pending = %+v, want 账号5678/138****5678/pending/state=nil", got)
	}

	// 诊断列表：无任务行的待审账号同样出现，且带账号状态。
	rec = e.do(http.MethodGet, "/api/me/diagnostics", nil, issued)
	if rec.Code != http.StatusOK {
		t.Fatalf("diagnostics status = %d: %s", rec.Code, rec.Body.String())
	}
	var diag readReport
	e.decode(rec, &diag)
	diagBody := rec.Body.String()
	diagByID := map[string]readItem{}
	for _, it := range diag.Items {
		diagByID[it.AccountID] = it
	}
	if got := diagByID[pendingID]; got.DisplayName != "账号5678" ||
		got.AccountStatus != string(coreaccount.StatusPending) || got.State != nil {
		t.Fatalf("diagnostics pending = %+v, want pending account with state=nil", got)
	}

	// 管理员读单账号详版：字段同源。
	admin := e.user("fe-state-admin@example.com", coreauth.RoleAdmin)
	e.bind(admin.ID, school.ID)
	adminIssued := e.login(admin)
	rec = e.do(http.MethodGet, "/api/accounts/"+pendingID+"/diagnostics", nil, adminIssued)
	if rec.Code != http.StatusOK {
		t.Fatalf("admin detail status = %d: %s", rec.Code, rec.Body.String())
	}
	var detail readItem
	e.decode(rec, &detail)
	detailBody := rec.Body.String()
	if detail.DisplayName != "账号5678" || detail.PhoneMasked != "138****5678" ||
		detail.AccountStatus != string(coreaccount.StatusPending) {
		t.Fatalf("admin detail = %+v", detail)
	}

	// 跨用户与跨校仍 404。
	other := e.user("fe-state-other@example.com", coreauth.RoleUser)
	if rec := e.do(http.MethodGet, "/api/accounts/"+activeID+"/diagnostics",
		nil, e.login(other)); rec.Code != http.StatusNotFound {
		t.Fatalf("跨用户 status = %d, want 404", rec.Code)
	}
	otherAdmin := e.user("fe-state-admin2@example.com", coreauth.RoleAdmin)
	e.bind(otherAdmin.ID, e.school("fe-state-other-school").ID)
	if rec := e.do(http.MethodGet, "/api/accounts/"+activeID+"/diagnostics",
		nil, e.login(otherAdmin)); rec.Code != http.StatusNotFound {
		t.Fatalf("跨校 admin status = %d, want 404", rec.Code)
	}

	// 完整手机号不出响应：两条原号明文都不出现。
	for name, body := range map[string]string{
		"sign-status":  statusBody,
		"diagnostics":  diagBody,
		"admin-detail": detailBody,
	} {
		for _, phone := range []string{"13800061234", "13800065678"} {
			if strings.Contains(body, phone) {
				t.Fatalf("%s 含完整手机号 %s: %s", name, phone, body)
			}
		}
	}
}
