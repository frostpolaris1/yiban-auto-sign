package main

// 一人一号结构不变量的全栈 e2e：真库、真登录、真 HTTP。
//
// # 功能
// 覆盖「每个属主名下最多一个 live 账号」对全部建号路径生效：
//   - admin 代建：属主已有 live 账号 → 409 conflict；
//   - user 自建：同属主 → 409；
//   - 自删（宽限期内）后重建 → 201：软删行不计 live；
//   - 随后自助恢复旧号 → 400：名下已有生效账号（既有语义）。
//
// # 归属
// cmd/server 是唯一可同时引用 adapters、app、entry 的位置。
// 环境与助手复用 auth_e2e_test.go 的 newAuthEnv 与 ux2 系列助手。
//
// # 口径
// 缺 DSN 时整包跳过并打印横幅，跳过不是绿。
// 手机号只用 synthetic-fixtures 登记值；邮箱一律 example.com。
// 本文件不测仓储层零行分类，那是 pg 包 quota e2e 的职责。

import (
	"encoding/json"
	"net/http"
	"testing"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

// 一人一号 e2e 的合成手机号，全部取自登记段（tests/synthetic-fixtures.json）。
const (
	onePhoneFirst    = "13800000060" // admin 代建的第一单
	onePhoneBlocked  = "13800000070" // 同属主第二次代建（期望 409）
	onePhoneSelfTry  = "13800000071" // user 自建撞 live 行（期望 409）
	onePhoneRecreate = "13800000072" // 自删后重建（期望 201）
)

// oneDecodeAccount 解出响应里的账号字段。
func oneDecodeAccount(t *testing.T, payload []byte) u2Account {
	t.Helper()
	var body u2Account
	if err := json.Unmarshal(payload, &body); err != nil {
		t.Fatalf("decode account: %v; body=%q", err, payload)
	}
	return body
}

func TestOneAccountPerOwner(t *testing.T) {
	e := newAuthEnv(t)
	schoolID := u2SeedSchool(t, e, "oneacct")

	admin := u2SeedUser(t, e, "oneacct-admin@example.com", "admin-pass", coreauth.RoleAdmin)
	u2BindSchool(t, e, admin.ID, schoolID)
	member := u2SeedUser(t, e, "oneacct-member@example.com", "member-pass", coreauth.RoleUser)

	adminClient := u2FreshClient(t)
	adminSession := u2LoginOn(e, adminClient, "oneacct-admin@example.com", "admin-pass")
	memberClient := u2FreshClient(t)
	memberSession := u2LoginOn(e, memberClient, "oneacct-member@example.com", "member-pass")

	// 代建第一单：201。
	resp, payload := u2CreateAccount(e, adminClient, adminSession.CSRFToken, u2CreateBody{
		SchoolID: schoolID, Phone: onePhoneFirst, Password: "member-pass-2", OwnerUserID: member.ID,
	})
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("first create = %d, want 201; body=%q", resp.StatusCode, payload)
	}
	first := oneDecodeAccount(t, payload)
	if first.OwnerUserID != member.ID {
		t.Fatalf("first owner = %q, want %q", first.OwnerUserID, member.ID)
	}

	// 同属主第二次代建：409 conflict。
	resp, payload = u2CreateAccount(e, adminClient, adminSession.CSRFToken, u2CreateBody{
		SchoolID: schoolID, Phone: onePhoneBlocked, Password: "member-pass-2", OwnerUserID: member.ID,
	})
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("second create = %d, want 409; body=%q", resp.StatusCode, payload)
	}
	if code, _ := errorCode(t, payload); code != "conflict" {
		t.Fatalf("second create code = %q, want conflict", code)
	}
	if got := e.scalarInt("SELECT count(*) FROM accounts WHERE phone = $1", onePhoneBlocked); got != 0 {
		t.Fatal("a rejected create must not leave an account row")
	}

	// user 自建同属主：同样 409。
	resp, payload = u2CreateAccount(e, memberClient, memberSession.CSRFToken, u2CreateBody{
		SchoolID: schoolID, Phone: onePhoneSelfTry, Password: "member-pass-2",
	})
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("user self create = %d, want 409; body=%q", resp.StatusCode, payload)
	}
	if code, _ := errorCode(t, payload); code != "conflict" {
		t.Fatalf("user self create code = %q, want conflict", code)
	}

	// 属主自删（宽限期内）：软删行不计 live，重建 201。
	delResp, delPayload := u1Do(e, memberClient, http.MethodPost,
		"/api/me/accounts/"+first.ID+"/delete", u1ReauthBody("member-pass"), memberSession.CSRFToken)
	if delResp.StatusCode != http.StatusNoContent {
		t.Fatalf("self delete = %d, want 204; body=%q", delResp.StatusCode, delPayload)
	}
	resp, payload = u2CreateAccount(e, memberClient, memberSession.CSRFToken, u2CreateBody{
		SchoolID: schoolID, Phone: onePhoneRecreate, Password: "member-pass-2",
	})
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("recreate after self delete = %d, want 201; body=%q", resp.StatusCode, payload)
	}

	// 既有语义：宽限期内恢复旧号，名下已有生效账号 → 400。
	resp, payload = u1Do(e, memberClient, http.MethodPost,
		"/api/me/accounts/"+first.ID+"/restore", u1ReauthBody("member-pass"), memberSession.CSRFToken)
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("restore old account = %d, want 400; body=%q", resp.StatusCode, payload)
	}
	if code, _ := errorCode(t, payload); code != "validation_failed" {
		t.Fatalf("restore old code = %q, want validation_failed", code)
	}
}
