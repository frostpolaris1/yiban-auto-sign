package main

// 账号响应里的机型 e2e：机型存在凭据密文里，列表与详情按需解密。
//
// # 功能
// 覆盖 master 建号回填机型、user 列表可见自己的机型、admin 与 user
// 的作用域不串、软删行与缺失凭据行留空且列表仍 200。
// 另钉住密码与设备码永不出现在响应体里。
//
// # 归属
// cmd/server 是唯一可同时引用 adapters、app、entry 的位置。
//
// # 口径
// 缺 DSN 时整包跳过并打可见横幅。跳过不是绿。
// 每个测试独占一个 schema，互不依赖。
// 手机号只取登记的合成段（tests/synthetic-fixtures.json）。

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

// accountModelAccount 是响应里与机型相关的账号字段。
// PhoneModel 用指针区分「字段缺席」与「空串」。
type accountModelAccount struct {
	ID          string  `json:"id"`
	SchoolID    string  `json:"school_id"`
	OwnerUserID string  `json:"owner_user_id"`
	PhoneModel  *string `json:"phone_model"`
}

type accountModelPage struct {
	Items []accountModelAccount `json:"items"`
}

// accountModelList 以指定客户端读一页账号。
func accountModelList(t *testing.T, e *authEnv, client *http.Client, query string) accountModelPage {
	t.Helper()
	resp, payload := u2Do(e, client, http.MethodGet, "/api/accounts"+query, nil, "")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("list accounts = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	var page accountModelPage
	if err := json.Unmarshal(payload, &page); err != nil {
		t.Fatalf("decode account page: %v; body=%q", err, payload)
	}
	return page
}

// accountModelRow 找一页里的某一行。
func accountModelRow(t *testing.T, page accountModelPage, id string) (accountModelAccount, bool) {
	t.Helper()
	for _, item := range page.Items {
		if item.ID == id {
			return item, true
		}
	}
	return accountModelAccount{}, false
}

// accountModelCreate 以指定客户端建号并返回响应行。
func accountModelCreate(t *testing.T, e *authEnv, client *http.Client, csrf string, body n190DeviceCreateBody) accountModelAccount {
	t.Helper()
	resp, payload := u2Do(e, client, http.MethodPost, "/api/accounts", body, csrf)
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("create account = %d, want 201; body=%q", resp.StatusCode, payload)
	}
	if strings.Contains(string(payload), "device_code") || strings.Contains(string(payload), "password") {
		t.Fatalf("create response leaked a credential field: %q", payload)
	}
	var created accountModelAccount
	if err := json.Unmarshal(payload, &created); err != nil {
		t.Fatalf("decode created account: %v; body=%q", err, payload)
	}
	return created
}

func TestAccountPhoneModelResponses(t *testing.T) {
	e := newAuthEnv(t)
	schoolA := u2SeedSchool(t, e, "model-a")
	schoolB := u2SeedSchool(t, e, "model-b")

	u2SeedUser(t, e, "model-master@example.com", "master-pass", coreauth.RoleMaster)
	member := u2SeedUser(t, e, "model-member@example.com", "member-pass", coreauth.RoleUser)
	outsider := u2SeedUser(t, e, "model-outsider@example.com", "outsider-pass", coreauth.RoleUser)
	admin := u2SeedUser(t, e, "model-admin@example.com", "admin-pass", coreauth.RoleAdmin)
	u2BindSchool(t, e, admin.ID, schoolA)

	masterSession := u2LoginOn(e, e.client, "model-master@example.com", "master-pass")
	memberClient := u2FreshClient(t)
	memberSession := u2LoginOn(e, memberClient, "model-member@example.com", "member-pass")
	adminClient := u2FreshClient(t)
	u2LoginOn(e, adminClient, "model-admin@example.com", "admin-pass")

	own := accountModelCreate(t, e, e.client, masterSession.CSRFToken, n190DeviceCreateBody{
		SchoolID:    schoolA,
		Phone:       "13800000030",
		Password:    "member-pass-2",
		OwnerUserID: member.ID,
		PhoneModel:  "Pixel 7",
	})
	if own.PhoneModel == nil || *own.PhoneModel != "Pixel 7" {
		t.Fatalf("create response phone_model = %v, want Pixel 7", own.PhoneModel)
	}

	foreign := accountModelCreate(t, e, e.client, masterSession.CSRFToken, n190DeviceCreateBody{
		SchoolID:    schoolB,
		Phone:       "13800000031",
		Password:    "outsider-pass-2",
		OwnerUserID: outsider.ID,
		PhoneModel:  "iPhone 13",
	})

	// master 全量：两行各自的机型都读到。
	page := accountModelList(t, e, e.client, "?limit=200")
	for id, want := range map[string]string{own.ID: "Pixel 7", foreign.ID: "iPhone 13"} {
		row, ok := accountModelRow(t, page, id)
		if !ok {
			t.Fatalf("master list is missing account %s", id)
		}
		if row.PhoneModel == nil || *row.PhoneModel != want {
			t.Fatalf("master list %s phone_model = %v, want %s", id, row.PhoneModel, want)
		}
	}

	// user 只看自己的账号：机型可见，另一属主的行不可见。
	userPage := accountModelList(t, e, memberClient, "?limit=200")
	ownRow, ok := accountModelRow(t, userPage, own.ID)
	if !ok {
		t.Fatal("the user list is missing the user's own account")
	}
	if ownRow.PhoneModel == nil || *ownRow.PhoneModel != "Pixel 7" {
		t.Fatalf("user list phone_model = %v, want Pixel 7", ownRow.PhoneModel)
	}
	if _, ok := accountModelRow(t, userPage, foreign.ID); ok {
		t.Fatal("the user list leaked another owner's account")
	}

	// admin 只绑定 schoolA：schoolB 的行不出现。
	adminPage := accountModelList(t, e, adminClient, "?limit=200")
	if _, ok := accountModelRow(t, adminPage, own.ID); !ok {
		t.Fatal("the admin list is missing a bound-school account")
	}
	if _, ok := accountModelRow(t, adminPage, foreign.ID); ok {
		t.Fatal("the admin list leaked an unbound-school account")
	}

	// 改机型：写回凭据，响应立即带新值，列表再读也一致。
	patchResp, patchPayload := u2Do(e, memberClient, http.MethodPatch, "/api/accounts/"+own.ID,
		map[string]string{"phone_model": "Pixel 8"}, memberSession.CSRFToken)
	if patchResp.StatusCode != http.StatusOK {
		t.Fatalf("update phone_model = %d, want 200; body=%q", patchResp.StatusCode, patchPayload)
	}
	var patched accountModelAccount
	if err := json.Unmarshal(patchPayload, &patched); err != nil {
		t.Fatalf("decode update response: %v; body=%q", err, patchPayload)
	}
	if patched.PhoneModel == nil || *patched.PhoneModel != "Pixel 8" {
		t.Fatalf("update response phone_model = %v, want Pixel 8", patched.PhoneModel)
	}
	page = accountModelList(t, e, e.client, "?limit=200")
	if row, ok := accountModelRow(t, page, own.ID); !ok || row.PhoneModel == nil || *row.PhoneModel != "Pixel 8" {
		t.Fatalf("list phone_model after update = %v, want Pixel 8", row.PhoneModel)
	}

	// 详情同样解密机型。
	detailResp, detailPayload := u2Do(e, memberClient, http.MethodGet, "/api/accounts/"+own.ID, nil, "")
	if detailResp.StatusCode != http.StatusOK {
		t.Fatalf("account detail = %d, want 200; body=%q", detailResp.StatusCode, detailPayload)
	}
	var detail accountModelAccount
	if err := json.Unmarshal(detailPayload, &detail); err != nil {
		t.Fatalf("decode account detail: %v; body=%q", err, detailPayload)
	}
	if detail.PhoneModel == nil || *detail.PhoneModel != "Pixel 8" {
		t.Fatalf("detail phone_model = %v, want Pixel 8", detail.PhoneModel)
	}

	// 另一条活行用来证明留空是逐行判定，不是整页清空。
	live := accountModelCreate(t, e, e.client, masterSession.CSRFToken, n190DeviceCreateBody{
		SchoolID:    schoolA,
		Phone:       "13800000032",
		Password:    "member-pass-3",
		OwnerUserID: member.ID,
		PhoneModel:  "Pixel 7",
	})

	// 软删行不读凭据：列表仍 200，该行机型为空，活行不受影响。
	resp, payload := u2Do(e, memberClient, http.MethodDelete, "/api/accounts/"+own.ID, nil, memberSession.CSRFToken)
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("user delete own account = %d, want 204; body=%q", resp.StatusCode, payload)
	}
	withDeleted := accountModelList(t, e, e.client, "?limit=200&include_deleted=true")
	deletedRow, ok := accountModelRow(t, withDeleted, own.ID)
	if !ok {
		t.Fatal("the include_deleted list is missing the soft-deleted account")
	}
	if deletedRow.PhoneModel != nil {
		t.Fatalf("soft-deleted row phone_model = %q, want empty", *deletedRow.PhoneModel)
	}
	livingRow, ok := accountModelRow(t, withDeleted, live.ID)
	if !ok || livingRow.PhoneModel == nil || *livingRow.PhoneModel != "Pixel 7" {
		t.Fatalf("live row phone_model = %v, want Pixel 7", livingRow.PhoneModel)
	}

	// 凭据行缺失：该行机型留空，列表仍是 200。
	if _, err := e.db.Exec(e.ctx, "DELETE FROM credentials WHERE account_id = $1::uuid", foreign.ID); err != nil {
		t.Fatalf("delete credential row: %v", err)
	}
	afterCredentialDelete := accountModelList(t, e, e.client, "?limit=200")
	foreignRow, ok := accountModelRow(t, afterCredentialDelete, foreign.ID)
	if !ok {
		t.Fatal("the list dropped an account whose credential is missing")
	}
	if foreignRow.PhoneModel != nil {
		t.Fatalf("credential-less row phone_model = %q, want empty", *foreignRow.PhoneModel)
	}
	livingRow, ok = accountModelRow(t, afterCredentialDelete, live.ID)
	if !ok || livingRow.PhoneModel == nil || *livingRow.PhoneModel != "Pixel 7" {
		t.Fatalf("live row phone_model after a peer lost its credential = %v, want Pixel 7", livingRow.PhoneModel)
	}

	// 详情对缺失凭据同样留空而不失败。
	missingResp, missingPayload := u2Do(e, e.client, http.MethodGet, "/api/accounts/"+foreign.ID, nil, "")
	if missingResp.StatusCode != http.StatusOK {
		t.Fatalf("credential-less detail = %d, want 200; body=%q", missingResp.StatusCode, missingPayload)
	}
	var missing accountModelAccount
	if err := json.Unmarshal(missingPayload, &missing); err != nil {
		t.Fatalf("decode credential-less detail: %v; body=%q", err, missingPayload)
	}
	if missing.PhoneModel != nil {
		t.Fatalf("credential-less detail phone_model = %q, want empty", *missing.PhoneModel)
	}
}
