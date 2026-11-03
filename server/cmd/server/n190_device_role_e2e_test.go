package main

// 设备字段的角色门 e2e：新线只有一个 /api/accounts，
// user 也走同一个写口，故设备识别码必须由用例层按角色把住。
//
// # 功能
// 覆盖 user 与 master 在 POST/PATCH /api/accounts 上的设备码差异、
// user 保留的机型/手机号/口令写口，以及 master 的清除设备码哨兵。
// 清除后用凭据明文确认设备码真的空了，而不是只看响应形状。
//
// # 归属
// cmd/server 是唯一可同时引用 adapters、app、entry 的位置。
//
// # 口径
// 缺 DSN 时整包跳过并打可见横幅。跳过不是绿。
// 每个测试独占一个 schema，互不依赖。
// 手机号只取登记的合成段（tests/synthetic-fixtures.json）。
// 设备码明文只在本进程内读一次，不进响应断言、不进日志。

import (
	"encoding/json"
	"net/http"
	"testing"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// 本文件的合成手机号全部取自登记段（tests/synthetic-fixtures.json）。
const (
	n190DevicePhoneUser    = "13900000006"
	n190DevicePhoneRebind  = "13900000007"
	n190DevicePhoneBlocked = "13900000008"
	n190DevicePhoneMaster  = "13900000009"
	n190DevicePhoneClear   = "13900000011"
)

// n190DeviceCreateBody 是带设备字段的建号请求体。
type n190DeviceCreateBody struct {
	SchoolID   string `json:"school_id"`
	Phone      string `json:"phone"`
	Password   string `json:"password"`
	DeviceCode string `json:"device_code,omitempty"`
	PhoneModel string `json:"phone_model,omitempty"`
}

// n190CredentialPayload 用测试钥解出账号当前凭据明文。
// 响应只给掩码，清除哨兵必须看密文背后的真实值。
func n190CredentialPayload(t *testing.T, e *authEnv, accountID string) appaccount.CredentialPayload {
	t.Helper()
	cipher, err := appaccount.NewCipher(authTestCredentialKey)
	if err != nil {
		t.Fatalf("build credential cipher: %v", err)
	}
	credential, err := pg.NewAccountRepo(e.db).Credential(e.ctx, corescope.Master(), accountID)
	if err != nil {
		t.Fatalf("load credential: %v", err)
	}
	payload, err := cipher.Open(credential)
	if err != nil {
		t.Fatalf("open credential: %v", err)
	}
	return payload
}

// n190DecodeAccount 解出建号响应里的 id。
func n190DecodeAccount(t *testing.T, payload []byte) u2Account {
	t.Helper()
	var created u2Account
	if err := json.Unmarshal(payload, &created); err != nil {
		t.Fatalf("decode account: %v; body=%q", err, payload)
	}
	return created
}

// n190PatchAccount 发一次带 CSRF 的账号 PATCH。
func n190PatchAccount(e *authEnv, client *http.Client, csrf, id string, body map[string]string) (*http.Response, []byte) {
	e.t.Helper()
	return u2Do(e, client, http.MethodPatch, "/api/accounts/"+id, body, csrf)
}

// TestN190DeviceRoleGate 覆盖 user 不得写设备码、其余字段仍可写，
// 以及 master 不受此限。
func TestN190DeviceRoleGate(t *testing.T) {
	e := newAuthEnv(t)
	schoolID := u2SeedSchool(t, e, "n190-device")

	u2SeedUser(t, e, "n190-device-master@example.com", "master-pass", coreauth.RoleMaster)
	member := u2SeedUser(t, e, "n190-device-member@example.com", "member-pass", coreauth.RoleUser)
	masterSession := u2LoginOn(e, e.client, "n190-device-master@example.com", "master-pass")
	memberClient := u2FreshClient(t)
	memberSession := u2LoginOn(e, memberClient, "n190-device-member@example.com", "member-pass")

	// user 建号带机型：201，机型写进凭据，设备码保持空。
	resp, payload := u2Do(e, memberClient, http.MethodPost, "/api/accounts", n190DeviceCreateBody{
		SchoolID:   schoolID,
		Phone:      n190DevicePhoneUser,
		Password:   "member-pass-2",
		PhoneModel: "Pixel 合成机型",
	}, memberSession.CSRFToken)
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("user create with phone_model = %d, want 201; body=%q", resp.StatusCode, payload)
	}
	own := n190DecodeAccount(t, payload)
	if own.OwnerUserID != member.ID {
		t.Fatalf("user create owner = %q, want %q", own.OwnerUserID, member.ID)
	}
	if got := n190CredentialPayload(t, e, own.ID); got.PhoneModel != "Pixel 合成机型" || got.DeviceCode != "" {
		t.Fatalf("credential after user create = %+v, want the model and an empty device code", got)
	}

	// user 改自己的设备码：403，且设备码没有被写进去。
	patchResp, patchPayload := n190PatchAccount(e, memberClient, memberSession.CSRFToken, own.ID,
		map[string]string{"device_code": "user-set-device"})
	if patchResp.StatusCode != http.StatusForbidden {
		t.Fatalf("user patch device_code = %d, want 403; body=%q", patchResp.StatusCode, patchPayload)
	}
	if code, _ := errorCode(t, patchPayload); code != "forbidden" {
		t.Fatalf("user patch device_code error = %q, want forbidden", code)
	}
	if got := n190CredentialPayload(t, e, own.ID); got.DeviceCode != "" {
		t.Fatalf("device code after the forbidden patch = %q, want empty", got.DeviceCode)
	}

	// user 改机型：200，凭据里机型更新。
	patchResp, patchPayload = n190PatchAccount(e, memberClient, memberSession.CSRFToken, own.ID,
		map[string]string{"phone_model": "Pixel"})
	if patchResp.StatusCode != http.StatusOK {
		t.Fatalf("user patch phone_model = %d, want 200; body=%q", patchResp.StatusCode, patchPayload)
	}
	if got := n190CredentialPayload(t, e, own.ID); got.PhoneModel != "Pixel" || got.DeviceCode != "" {
		t.Fatalf("credential after user model patch = %+v, want Pixel and an empty device code", got)
	}

	// user 改绑手机号：200。
	patchResp, patchPayload = n190PatchAccount(e, memberClient, memberSession.CSRFToken, own.ID,
		map[string]string{"phone": n190DevicePhoneRebind})
	if patchResp.StatusCode != http.StatusOK {
		t.Fatalf("user patch phone = %d, want 200; body=%q", patchResp.StatusCode, patchPayload)
	}

	// user 改口令：200。
	patchResp, patchPayload = n190PatchAccount(e, memberClient, memberSession.CSRFToken, own.ID,
		map[string]string{"password": "member-pass-3"})
	if patchResp.StatusCode != http.StatusOK {
		t.Fatalf("user patch password = %d, want 200; body=%q", patchResp.StatusCode, patchPayload)
	}

	// user 建号带设备码：403，且没有落任何账号行。
	resp, payload = u2Do(e, memberClient, http.MethodPost, "/api/accounts", n190DeviceCreateBody{
		SchoolID:   schoolID,
		Phone:      n190DevicePhoneBlocked,
		Password:   "member-pass-2",
		DeviceCode: "user-create-device",
	}, memberSession.CSRFToken)
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("user create with device_code = %d, want 403; body=%q", resp.StatusCode, payload)
	}
	if got := e.scalarInt("SELECT count(*) FROM accounts WHERE phone = $1", n190DevicePhoneBlocked); got != 0 {
		t.Fatal("a create rejected for the role gate must not leave an account row")
	}

	// master 建号带设备码：201，设备码写进凭据。
	resp, payload = u2Do(e, e.client, http.MethodPost, "/api/accounts", n190DeviceCreateBody{
		SchoolID:   schoolID,
		Phone:      n190DevicePhoneMaster,
		Password:   "master-pass-2",
		DeviceCode: "master-create-device",
	}, masterSession.CSRFToken)
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("master create with device_code = %d, want 201; body=%q", resp.StatusCode, payload)
	}
	masterAccount := n190DecodeAccount(t, payload)
	if got := n190CredentialPayload(t, e, masterAccount.ID); got.DeviceCode != "master-create-device" {
		t.Fatalf("device code after master create = %q, want master-create-device", got.DeviceCode)
	}

	// master 改设备码：200。
	patchResp, patchPayload = n190PatchAccount(e, e.client, masterSession.CSRFToken, masterAccount.ID,
		map[string]string{"device_code": "master-patch-device"})
	if patchResp.StatusCode != http.StatusOK {
		t.Fatalf("master patch device_code = %d, want 200; body=%q", patchResp.StatusCode, patchPayload)
	}
	if got := n190CredentialPayload(t, e, masterAccount.ID); got.DeviceCode != "master-patch-device" {
		t.Fatalf("device code after master patch = %q, want master-patch-device", got.DeviceCode)
	}
}

// TestN190DeviceClearSentinel 覆盖 master 用空串清除设备码的哨兵：
// 清除后凭据里设备码为空，可以再由 device-refresh 重新登记。
func TestN190DeviceClearSentinel(t *testing.T) {
	e := newAuthEnv(t)
	schoolID := u2SeedSchool(t, e, "n190-device-clear")

	u2SeedUser(t, e, "n190-clear-master@example.com", "master-pass", coreauth.RoleMaster)
	masterSession := u2LoginOn(e, e.client, "n190-clear-master@example.com", "master-pass")

	resp, payload := u2Do(e, e.client, http.MethodPost, "/api/accounts", n190DeviceCreateBody{
		SchoolID:   schoolID,
		Phone:      n190DevicePhoneClear,
		Password:   "master-pass-2",
		DeviceCode: "device-to-clear",
		PhoneModel: "Mi 9 合成机型",
	}, masterSession.CSRFToken)
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("master create = %d, want 201; body=%q", resp.StatusCode, payload)
	}
	created := n190DecodeAccount(t, payload)
	if got := n190CredentialPayload(t, e, created.ID); got.DeviceCode != "device-to-clear" {
		t.Fatalf("seeded device code = %q, want device-to-clear", got.DeviceCode)
	}

	// 空串是清除哨兵：200，设备码为空，机型保持不变。
	patchResp, patchPayload := n190PatchAccount(e, e.client, masterSession.CSRFToken, created.ID,
		map[string]string{"device_code": ""})
	if patchResp.StatusCode != http.StatusOK {
		t.Fatalf("master clear device_code = %d, want 200; body=%q", patchResp.StatusCode, patchPayload)
	}
	cleared := n190CredentialPayload(t, e, created.ID)
	if cleared.DeviceCode != "" {
		t.Fatalf("device code after clear = %q, want empty", cleared.DeviceCode)
	}
	if cleared.PhoneModel != "Mi 9 合成机型" {
		t.Fatalf("phone model after clear = %q, want the previous value", cleared.PhoneModel)
	}
}
