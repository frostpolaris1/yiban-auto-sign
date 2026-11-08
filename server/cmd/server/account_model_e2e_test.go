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
	"unicode/utf8"

	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
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

// accountModelText 把可缺省的机型变成失败信息可读的文本。
func accountModelText(value *string) string {
	if value == nil {
		return "<absent>"
	}
	return *value
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
		DeviceCode:  "device-secret-190",
		PhoneModel:  "Pixel 7",
	})
	if accountModelText(own.PhoneModel) != "Pixel 7" {
		t.Fatalf("create response phone_model = %s, want Pixel 7", accountModelText(own.PhoneModel))
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
		if accountModelText(row.PhoneModel) != want {
			t.Fatalf("master list %s phone_model = %s, want %s", id, accountModelText(row.PhoneModel), want)
		}
	}

	// 列表原始响应体不得出现凭据明文。
	listResp, rawList := u2Do(e, e.client, http.MethodGet, "/api/accounts?limit=200", nil, "")
	if listResp.StatusCode != http.StatusOK {
		t.Fatalf("raw list = %d, want 200; body=%q", listResp.StatusCode, rawList)
	}
	for _, secret := range []string{"member-pass-2", "outsider-pass-2", "device-secret-190"} {
		if strings.Contains(string(rawList), secret) {
			t.Fatalf("the list body leaked the credential value %q", secret)
		}
	}

	// user 只看自己的账号：机型可见，另一属主的行不可见。
	userPage := accountModelList(t, e, memberClient, "?limit=200")
	ownRow, ok := accountModelRow(t, userPage, own.ID)
	if !ok {
		t.Fatal("the user list is missing the user's own account")
	}
	if accountModelText(ownRow.PhoneModel) != "Pixel 7" {
		t.Fatalf("user list phone_model = %s, want Pixel 7", accountModelText(ownRow.PhoneModel))
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
	if accountModelText(patched.PhoneModel) != "Pixel 8" {
		t.Fatalf("update response phone_model = %s, want Pixel 8", accountModelText(patched.PhoneModel))
	}
	page = accountModelList(t, e, e.client, "?limit=200")
	if row, ok := accountModelRow(t, page, own.ID); !ok || accountModelText(row.PhoneModel) != "Pixel 8" {
		t.Fatalf("list phone_model after update = %s, want Pixel 8", accountModelText(row.PhoneModel))
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
	if accountModelText(detail.PhoneModel) != "Pixel 8" {
		t.Fatalf("detail phone_model = %s, want Pixel 8", accountModelText(detail.PhoneModel))
	}
	for _, secret := range []string{"member-pass-2", "device-secret-190"} {
		if strings.Contains(string(detailPayload), secret) {
			t.Fatalf("the detail body leaked the credential value %q", secret)
		}
	}

	// 只改备注名：响应也带可读机型。
	renameResp, renamePayload := u2Do(e, memberClient, http.MethodPatch, "/api/accounts/"+own.ID,
		map[string]string{"name": "改个备注"}, memberSession.CSRFToken)
	if renameResp.StatusCode != http.StatusOK {
		t.Fatalf("rename = %d, want 200; body=%q", renameResp.StatusCode, renamePayload)
	}
	var renamed accountModelAccount
	if err := json.Unmarshal(renamePayload, &renamed); err != nil {
		t.Fatalf("decode rename response: %v; body=%q", err, renamePayload)
	}
	if accountModelText(renamed.PhoneModel) != "Pixel 8" {
		t.Fatalf("rename response phone_model = %s, want Pixel 8", accountModelText(renamed.PhoneModel))
	}

	// 审核、暂停与恢复的响应共用 Account schema，都必须带机型。
	reviewResp, reviewPayload := u2Do(e, e.client, http.MethodPost, "/api/accounts/"+own.ID+"/review",
		map[string]string{"decision": "approve"}, masterSession.CSRFToken)
	if reviewResp.StatusCode != http.StatusOK {
		t.Fatalf("review = %d, want 200; body=%q", reviewResp.StatusCode, reviewPayload)
	}
	var reviewed accountModelAccount
	if err := json.Unmarshal(reviewPayload, &reviewed); err != nil {
		t.Fatalf("decode review response: %v; body=%q", err, reviewPayload)
	}
	if accountModelText(reviewed.PhoneModel) != "Pixel 8" {
		t.Fatalf("review response phone_model = %s, want Pixel 8", accountModelText(reviewed.PhoneModel))
	}

	pauseResp, pausePayload := u2Do(e, e.client, http.MethodPost, "/api/accounts/"+own.ID+"/pause",
		map[string]bool{"paused": true}, masterSession.CSRFToken)
	if pauseResp.StatusCode != http.StatusOK {
		t.Fatalf("pause = %d, want 200; body=%q", pauseResp.StatusCode, pausePayload)
	}
	var paused accountModelAccount
	if err := json.Unmarshal(pausePayload, &paused); err != nil {
		t.Fatalf("decode pause response: %v; body=%q", err, pausePayload)
	}
	if accountModelText(paused.PhoneModel) != "Pixel 8" {
		t.Fatalf("pause response phone_model = %s, want Pixel 8", accountModelText(paused.PhoneModel))
	}

	resumeResp, resumePayload := u2Do(e, e.client, http.MethodPost, "/api/accounts/"+own.ID+"/pause",
		map[string]bool{"paused": false}, masterSession.CSRFToken)
	if resumeResp.StatusCode != http.StatusOK {
		t.Fatalf("resume = %d, want 200; body=%q", resumeResp.StatusCode, resumePayload)
	}
	var resumed accountModelAccount
	if err := json.Unmarshal(resumePayload, &resumed); err != nil {
		t.Fatalf("decode resume response: %v; body=%q", err, resumePayload)
	}
	if accountModelText(resumed.PhoneModel) != "Pixel 8" {
		t.Fatalf("resume response phone_model = %s, want Pixel 8", accountModelText(resumed.PhoneModel))
	}

	// 另一条活行用来证明留空是逐行判定，不是整页清空。
	// 一人一号：该行配独立属主，member 名下保持只有 own 一行，
	// 后面 member 自删 own 后 master 才能恢复它。
	liveOwner := u2SeedUser(t, e, "model-live-owner@example.com", "live-owner-pass", coreauth.RoleUser)
	live := accountModelCreate(t, e, e.client, masterSession.CSRFToken, n190DeviceCreateBody{
		SchoolID:    schoolA,
		Phone:       "13800000032",
		Password:    "member-pass-3",
		OwnerUserID: liveOwner.ID,
		PhoneModel:  "Pixel 7",
	})

	// 软删行不读凭据：列表仍 200，该行机型为空，活行不受影响。
	resp, payload := u2Do(e, memberClient, http.MethodDelete, "/api/accounts/"+own.ID,
		u1ReauthBody("member-pass"), memberSession.CSRFToken)
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("user delete own account = %d, want 204; body=%q", resp.StatusCode, payload)
	}
	withDeleted := accountModelList(t, e, e.client, "?limit=200&include_deleted=true")
	deletedRow, ok := accountModelRow(t, withDeleted, own.ID)
	if !ok {
		t.Fatal("the include_deleted list is missing the soft-deleted account")
	}
	if deletedRow.PhoneModel != nil {
		t.Fatalf("soft-deleted row phone_model = %q, want empty", accountModelText(deletedRow.PhoneModel))
	}
	livingRow, ok := accountModelRow(t, withDeleted, live.ID)
	if !ok || accountModelText(livingRow.PhoneModel) != "Pixel 7" {
		t.Fatalf("live row phone_model = %s, want Pixel 7", accountModelText(livingRow.PhoneModel))
	}

	// 恢复响应同样带机型：凭据行仍在，软删只挡列表读取。
	restoreResp, restorePayload := u2Do(e, e.client, http.MethodPost, "/api/accounts/"+own.ID+"/restore",
		u1ReauthBody("master-pass"), masterSession.CSRFToken)
	if restoreResp.StatusCode != http.StatusOK {
		t.Fatalf("restore = %d, want 200; body=%q", restoreResp.StatusCode, restorePayload)
	}
	var restored accountModelAccount
	if err := json.Unmarshal(restorePayload, &restored); err != nil {
		t.Fatalf("decode restore response: %v; body=%q", err, restorePayload)
	}
	if accountModelText(restored.PhoneModel) != "Pixel 8" {
		t.Fatalf("restore response phone_model = %s, want Pixel 8", accountModelText(restored.PhoneModel))
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
		t.Fatalf("credential-less row phone_model = %q, want empty", accountModelText(foreignRow.PhoneModel))
	}
	livingRow, ok = accountModelRow(t, afterCredentialDelete, live.ID)
	if !ok || accountModelText(livingRow.PhoneModel) != "Pixel 7" {
		t.Fatalf("live row phone_model after a peer lost its credential = %s, want Pixel 7", accountModelText(livingRow.PhoneModel))
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
		t.Fatalf("credential-less detail phone_model = %q, want empty", accountModelText(missing.PhoneModel))
	}

	// 密文损坏：解密失败只让该行机型留空，列表与详情仍是 200。
	if _, err := e.db.Exec(e.ctx,
		`UPDATE credentials SET ciphertext = decode(repeat('00', 32), 'hex') WHERE account_id = $1::uuid`,
		live.ID); err != nil {
		t.Fatalf("corrupt credential: %v", err)
	}
	corruptPage := accountModelList(t, e, e.client, "?limit=200")
	corruptRow, ok := accountModelRow(t, corruptPage, live.ID)
	if !ok {
		t.Fatal("the list dropped an account whose credential is unreadable")
	}
	if corruptRow.PhoneModel != nil {
		t.Fatalf("corrupt row phone_model = %q, want empty", accountModelText(corruptRow.PhoneModel))
	}
	if row, ok := accountModelRow(t, corruptPage, own.ID); !ok || accountModelText(row.PhoneModel) != "Pixel 8" {
		t.Fatalf("a peer row lost its model after a corrupt credential: %s", accountModelText(row.PhoneModel))
	}
	// live 属独立属主：这一步用 master 会话读，断言只关于坏凭据的留空行为。
	corruptResp, corruptPayload := u2Do(e, e.client, http.MethodGet, "/api/accounts/"+live.ID, nil, masterSession.CSRFToken)
	if corruptResp.StatusCode != http.StatusOK {
		t.Fatalf("corrupt-credential detail = %d, want 200; body=%q", corruptResp.StatusCode, corruptPayload)
	}
	var corrupt accountModelAccount
	if err := json.Unmarshal(corruptPayload, &corrupt); err != nil {
		t.Fatalf("decode corrupt-credential detail: %v; body=%q", err, corruptPayload)
	}
	if corrupt.PhoneModel != nil {
		t.Fatalf("corrupt-credential detail phone_model = %q, want empty", accountModelText(corrupt.PhoneModel))
	}
}

// TestAccountDeviceRefreshTruncatesLongPhoneModel 钉住平台回读也是写入路径：
// 超长机型按 rune 截到契约上限，响应与密文一致。
func TestAccountDeviceRefreshTruncatesLongPhoneModel(t *testing.T) {
	e := u2NewEnv(t)
	schoolID := e.seedSchool("model-long")
	master := e.seedUser("model-long-master@example.com", coreauth.RoleMaster)
	owner := e.seedUser("model-long-owner@example.com", coreauth.RoleUser)
	accountID := e.seedAccount(schoolID, owner.ID, u2DevicePhoneOwn, "SEEDDEV01", "")

	longModel := strings.Repeat("机", 70)
	e.reader.script = []u2DeviceOutcome{{
		state: appaccount.DeviceState{Authorized: true, PhoneModel: longModel},
	}}
	code, payload := e.do(http.MethodPost, "/api/accounts/"+accountID+"/device-refresh",
		u1ReauthBody(w2cSeedPassword), e.issue(master))
	if code != http.StatusOK {
		t.Fatalf("device refresh = %d, want 200; body=%q", code, payload)
	}
	var device struct {
		PhoneModel string `json:"phone_model"`
	}
	if err := json.Unmarshal(payload, &device); err != nil {
		t.Fatalf("decode device refresh: %v; body=%q", err, payload)
	}
	want := strings.Repeat("机", 64)
	if device.PhoneModel != want {
		t.Fatalf("refreshed phone_model has %d runes, want 64", utf8.RuneCountInString(device.PhoneModel))
	}
	if got := e.credentialPayload(accountID); got.PhoneModel != want {
		t.Fatalf("stored phone_model has %d runes, want 64", utf8.RuneCountInString(got.PhoneModel))
	}
}
