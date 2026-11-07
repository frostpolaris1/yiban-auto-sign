package main

// 用户批量动作端点的端到端测试。真库、真 HTTP、真 cookie。
//
// # 功能
// 钉住 POST /api/users/batch 的验收链：master 批量停用/重置/软删全 ok
// 且逐条审计、混合成败按序给出 per-item 稳定码、admin/user 整批 403
// 零写入、二次口令失败零写入、请求级校验 400、自删与自停用按 per-item
// 处理、恢复撞活跃邮箱 per-item conflict。
// 本文件**不**复制入口层替身单测。
//
// # 归属
// cmd/server 是唯一可同时引用 adapters、app、entry 的位置。
// 环境复用 auth_e2e_test.go 的 newAuthEnv 与 ux2 的种子帮手。
//
// # 口径
// 缺 DSN 时整包跳过并打可见横幅。跳过不是绿。
// 每个测试独占一个 schema，互不依赖。
// 邮箱只取合成值；本文件不造手机号。
// 审计计数一律按 target_id：成功项各一条、失败项零条。

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"testing"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

// userBatchMissingID 是一个形状合法但不存在的用户 id（零前缀合成段）。
const userBatchMissingID = "00000000-0000-4000-8000-0000000000a1"

// userBatchItem 与 userBatchResult 是用户批量响应里用到的字段。
type userBatchItem struct {
	ID           string  `json:"id"`
	Status       string  `json:"status"`
	Code         *string `json:"code"`
	Message      *string `json:"message"`
	TempPassword *string `json:"temp_password"`
}

type userBatchResult struct {
	Items []userBatchItem `json:"items"`
}

// userBatchDo 以指定客户端发一次用户批量请求并返回状态码与响应体。
func userBatchDo(t *testing.T, e *authEnv, client *http.Client, csrf, password, action string, ids []string) (*http.Response, []byte) {
	t.Helper()
	body := map[string]any{"action": action, "ids": ids, "current_password": password}
	return u2Do(e, client, http.MethodPost, "/api/users/batch", body, csrf)
}

// userBatchDecode 解码用户批量响应，失败即终止。
// 失败信息不带响应体：body 可能携带一次性 temp_password。
func userBatchDecode(t *testing.T, payload []byte) userBatchResult {
	t.Helper()
	var out userBatchResult
	if err := json.Unmarshal(payload, &out); err != nil {
		t.Fatalf("decode user batch result: %v", err)
	}
	return out
}

// userBatchItemShape 回显条目的 id|status|code 形状，供失败信息使用：
// 不回显 temp_password 与 message，避免一次性口令进测试日志。
func userBatchItemShape(item userBatchItem) string {
	shape := item.ID + "|" + item.Status
	if item.Code != nil {
		shape += "|" + *item.Code
	}
	return shape
}

// userBatchAuditCount 数某 target 的全部审计行。
func userBatchAuditCount(t *testing.T, e *authEnv, targetID string) int64 {
	t.Helper()
	return e.scalarInt("SELECT count(*) FROM audit_logs WHERE target_id = $1", targetID)
}

// userBatchAuditAction 数某 target 的指定动作审计行。
func userBatchAuditAction(t *testing.T, e *authEnv, targetID, action string) int64 {
	t.Helper()
	return e.scalarInt(
		"SELECT count(*) FROM audit_logs WHERE target_id = $1 AND action = $2", targetID, action)
}

// userBatchStatus 读用户的库内状态；deletedAt 为真时读软删行，否则读活行。
func userBatchStatus(t *testing.T, e *authEnv, id string, deletedAt bool) string {
	t.Helper()
	sql := "SELECT coalesce(status::text, '') FROM users WHERE id = $1"
	if deletedAt {
		sql += " AND deleted_at IS NOT NULL"
	} else {
		sql += " AND deleted_at IS NULL"
	}
	return e.scalarString(sql, id)
}

// 验收①：master 批量停用两个、重置口令一个、软删一个，全部 ok，
// 审计逐条存在，库内状态逐条落位；temp_password 非空且只出现一次。
func TestUserBatchMasterAllOk(t *testing.T) {
	e := newAuthEnv(t)

	u2SeedUser(t, e, "ubatch-all-master@example.com", "master-pass", coreauth.RoleMaster)
	masterSession := u2LoginOn(e, e.client, "ubatch-all-master@example.com", "master-pass")
	disableA := w2dSeedUser(t, e, "ubatch-all-a@example.com", "user-pass", coreauth.RoleUser)
	disableB := w2dSeedUser(t, e, "ubatch-all-b@example.com", "user-pass", coreauth.RoleUser)
	toReset := w2dSeedUser(t, e, "ubatch-all-reset@example.com", "user-pass", coreauth.RoleUser)
	toDelete := w2dSeedUser(t, e, "ubatch-all-del@example.com", "user-pass", coreauth.RoleUser)

	// 批量停用：两个普通用户一起转 disabled。
	resp, payload := userBatchDo(t, e, e.client, masterSession.CSRFToken, "master-pass",
		"disable", []string{disableA.ID, disableB.ID})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("batch disable = %d, want 200", resp.StatusCode)
	}
	result := userBatchDecode(t, payload)
	if len(result.Items) != 2 {
		t.Fatalf("disable items = %d, want 2", len(result.Items))
	}
	for i, id := range []string{disableA.ID, disableB.ID} {
		item := result.Items[i]
		if item.ID != id || item.Status != "ok" || item.Code != nil || item.TempPassword != nil {
			t.Fatalf("disable items[%d] = %s, want ok without extras", i, userBatchItemShape(item))
		}
		if got := userBatchStatus(t, e, id, false); got != "disabled" {
			t.Fatalf("status of %s = %q, want disabled", id, got)
		}
		if got := userBatchAuditAction(t, e, id, "user.disable"); got != 1 {
			t.Fatalf("disable audit rows for %s = %d, want 1", id, got)
		}
	}

	// 批量重置口令：temp_password 非空，键名与口令值在响应体各只出现一次。
	resp, payload = userBatchDo(t, e, e.client, masterSession.CSRFToken, "master-pass",
		"reset_password", []string{toReset.ID})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("batch reset = %d, want 200", resp.StatusCode)
	}
	if got := strings.Count(string(payload), "temp_password"); got != 1 {
		t.Fatalf("the temp_password key appears %d times, want exactly once", got)
	}
	result = userBatchDecode(t, payload)
	if len(result.Items) != 1 || result.Items[0].ID != toReset.ID || result.Items[0].Status != "ok" {
		shapes := make([]string, 0, len(result.Items))
		for _, item := range result.Items {
			shapes = append(shapes, userBatchItemShape(item))
		}
		t.Fatalf("reset items = %v, want one ok for %s", shapes, toReset.ID)
	}
	if result.Items[0].TempPassword == nil || *result.Items[0].TempPassword == "" {
		t.Fatalf("reset item = %s, want a non-empty temp password",
			userBatchItemShape(result.Items[0]))
	}
	// 口令值本身在响应体只出现一次：值是唯一的一次性展示，键名计数
	// 抓不到「同值复用」。失败信息不回显口令值。
	if got := strings.Count(string(payload), *result.Items[0].TempPassword); got != 1 {
		t.Fatalf("the temp secret value appears %d times in the body, want exactly once", got)
	}
	if got := userBatchAuditAction(t, e, toReset.ID, "user.password_reset"); got != 1 {
		t.Fatalf("reset audit rows for %s = %d, want 1", toReset.ID, got)
	}

	// 批量软删：deleted_at 落库并写 user.delete 审计。
	resp, payload = userBatchDo(t, e, e.client, masterSession.CSRFToken, "master-pass",
		"delete", []string{toDelete.ID})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("batch delete = %d, want 200", resp.StatusCode)
	}
	result = userBatchDecode(t, payload)
	if len(result.Items) != 1 || result.Items[0].ID != toDelete.ID || result.Items[0].Status != "ok" {
		t.Fatalf("delete items = %d, want one ok for %s", len(result.Items), toDelete.ID)
	}
	if got := userBatchStatus(t, e, toDelete.ID, true); got == "" {
		t.Fatalf("the deleted user %s has no soft-deleted row", toDelete.ID)
	}
	if got := userBatchAuditAction(t, e, toDelete.ID, "user.delete"); got != 1 {
		t.Fatalf("delete audit rows for %s = %d, want 1", toDelete.ID, got)
	}
}

// 验收②：混合成败按序返回。停用三个 id：不存在 → not_found，
// 唯一 active master 停用自己 → conflict，普通用户 → ok；
// 随后一条正常 enable 批把被停用项恢复为 ok。
func TestUserBatchMixedPerItem(t *testing.T) {
	e := newAuthEnv(t)

	u2SeedUser(t, e, "ubatch-mix-master@example.com", "master-pass", coreauth.RoleMaster)
	masterSession := u2LoginOn(e, e.client, "ubatch-mix-master@example.com", "master-pass")
	masterID := masterSession.ID
	normal := w2dSeedUser(t, e, "ubatch-mix-normal@example.com", "user-pass", coreauth.RoleUser)

	resp, payload := userBatchDo(t, e, e.client, masterSession.CSRFToken, "master-pass",
		"disable", []string{userBatchMissingID, masterID, normal.ID})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("mixed disable = %d, want 200", resp.StatusCode)
	}
	result := userBatchDecode(t, payload)
	if len(result.Items) != 3 {
		t.Fatalf("mixed items = %d, want 3", len(result.Items))
	}
	first := result.Items[0]
	if first.ID != userBatchMissingID || first.Status != "error" || first.Code == nil || *first.Code != "not_found" {
		t.Fatalf("items[0] = %s, want error not_found", userBatchItemShape(first))
	}
	second := result.Items[1]
	if second.ID != masterID || second.Status != "error" || second.Code == nil || *second.Code != "conflict" {
		t.Fatalf("items[1] = %s, want error conflict for the last active master", userBatchItemShape(second))
	}
	third := result.Items[2]
	if third.ID != normal.ID || third.Status != "ok" || third.Code != nil {
		t.Fatalf("items[2] = %s, want ok", userBatchItemShape(third))
	}

	// 失败项零审计；成功项恰好一条停用审计，库内状态落位。
	if got := userBatchAuditCount(t, e, userBatchMissingID); got != 0 {
		t.Fatalf("audit rows for the missing id = %d, want 0", got)
	}
	if got := userBatchAuditCount(t, e, masterID); got != 0 {
		t.Fatalf("audit rows for the self-disable item = %d, want 0", got)
	}
	if got := userBatchAuditAction(t, e, normal.ID, "user.disable"); got != 1 {
		t.Fatalf("disable audit rows for %s = %d, want 1", normal.ID, got)
	}
	if got := userBatchStatus(t, e, normal.ID, false); got != "disabled" {
		t.Fatalf("status of %s = %q, want disabled", normal.ID, got)
	}

	// 正常 enable：把被停用的普通用户恢复为 active，逐条审计。
	resp, payload = userBatchDo(t, e, e.client, masterSession.CSRFToken, "master-pass",
		"enable", []string{normal.ID})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("enable batch = %d, want 200", resp.StatusCode)
	}
	result = userBatchDecode(t, payload)
	if len(result.Items) != 1 || result.Items[0].ID != normal.ID || result.Items[0].Status != "ok" {
		t.Fatalf("enable items = %d, want one ok for %s", len(result.Items), normal.ID)
	}
	if got := userBatchStatus(t, e, normal.ID, false); got != "active" {
		t.Fatalf("status of %s = %q, want active after enable", normal.ID, got)
	}
	if got := userBatchAuditAction(t, e, normal.ID, "user.enable"); got != 1 {
		t.Fatalf("enable audit rows for %s = %d, want 1", normal.ID, got)
	}
}

// 验收③：admin 与 user 整批 403，零写入零审计。
// 二次口令用各自正确值，保证 403 来自角色门槛而非 reauth 门。
func TestUserBatchForbiddenRoles(t *testing.T) {
	e := newAuthEnv(t)
	schoolID := u2SeedSchool(t, e, "ubatch-f")

	u2SeedUser(t, e, "ubatch-f-master@example.com", "master-pass", coreauth.RoleMaster)
	admin := u2SeedUser(t, e, "ubatch-f-admin@example.com", "admin-pass", coreauth.RoleAdmin)
	w2dSeedUser(t, e, "ubatch-f-member@example.com", "user-pass", coreauth.RoleUser)
	u2BindSchool(t, e, admin.ID, schoolID)
	masterSession := u2LoginOn(e, e.client, "ubatch-f-master@example.com", "master-pass")
	adminClient := u2FreshClient(t)
	adminSession := u2LoginOn(e, adminClient, "ubatch-f-admin@example.com", "admin-pass")
	memberClient := u2FreshClient(t)
	memberSession := u2LoginOn(e, memberClient, "ubatch-f-member@example.com", "user-pass")

	cases := []struct {
		name     string
		client   *http.Client
		session  sessionUserBody
		password string
	}{
		{"admin", adminClient, adminSession, "admin-pass"},
		{"user", memberClient, memberSession, "user-pass"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			resp, payload := userBatchDo(t, e, tc.client, tc.session.CSRFToken,
				tc.password, "disable", []string{masterSession.ID})
			if resp.StatusCode != http.StatusForbidden {
				t.Fatalf("%s batch = %d, want 403", tc.name, resp.StatusCode)
			}
			if code, _ := errorCode(t, payload); code != "forbidden" {
				t.Fatalf("%s code = %q, want forbidden", tc.name, code)
			}
		})
	}

	// 零写入零审计：master 行未动，无 user.disable 行。
	if got := userBatchStatus(t, e, masterSession.ID, false); got != "active" {
		t.Fatalf("master status after rejections = %q, want active", got)
	}
	if got := userBatchAuditAction(t, e, masterSession.ID, "user.disable"); got != 0 {
		t.Fatalf("disable audit rows after rejections = %d, want 0", got)
	}
}

// 验收④：二次口令缺失 400、错误 403，零写入零审计。
func TestUserBatchReauthGate(t *testing.T) {
	e := newAuthEnv(t)

	u2SeedUser(t, e, "ubatch-reauth-master@example.com", "master-pass", coreauth.RoleMaster)
	masterSession := u2LoginOn(e, e.client, "ubatch-reauth-master@example.com", "master-pass")
	target := w2dSeedUser(t, e, "ubatch-reauth-target@example.com", "user-pass", coreauth.RoleUser)

	before := userBatchAuditCount(t, e, target.ID)

	// 缺口令：400。
	resp, payload := userBatchDo(t, e, e.client, masterSession.CSRFToken, "",
		"disable", []string{target.ID})
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("missing password = %d, want 400", resp.StatusCode)
	}
	if code, _ := errorCode(t, payload); code != "validation_failed" {
		t.Fatalf("missing password code = %q, want validation_failed", code)
	}

	// 错误口令：403 forbidden 信封。
	resp, payload = userBatchDo(t, e, e.client, masterSession.CSRFToken, "wrong-pass-42",
		"disable", []string{target.ID})
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("wrong password = %d, want 403", resp.StatusCode)
	}
	if code, _ := errorCode(t, payload); code != "forbidden" {
		t.Fatalf("wrong password code = %q, want forbidden", code)
	}

	// 两次失败都零写入：状态不变、无新增审计。
	if got := userBatchStatus(t, e, target.ID, false); got != "active" {
		t.Fatalf("status after failures = %q, want active", got)
	}
	if got := userBatchAuditCount(t, e, target.ID); got != before {
		t.Fatalf("audit rows after failures = %d, want %d", got, before)
	}
}

// 验收⑤：请求级校验整批拒绝。11 个 id、重复 id、空 ids、未知动作都
// 400 validation_failed；失败零副作用：真实目标用户状态与审计不变。
func TestUserBatchValidationErrors(t *testing.T) {
	e := newAuthEnv(t)
	u2SeedUser(t, e, "ubatch-validate-master@example.com", "master-pass", coreauth.RoleMaster)
	masterSession := u2LoginOn(e, e.client, "ubatch-validate-master@example.com", "master-pass")
	// 真实目标用户：重复与未知动作两个判据都用它入批，
	// 校验拒绝后逐项核对状态与审计零变化。
	target := w2dSeedUser(t, e, "ubatch-validate-target@example.com", "user-pass", coreauth.RoleUser)

	// 11 个互不相同的合法形状 id：命中的只能是 >10，
	// 不会先撞上重复判据，让「超限」与「重复」两个判据各自可辨。
	eleven := make([]string, 11)
	for i := range eleven {
		eleven[i] = fmt.Sprintf("00000000-0000-4000-8000-%012d", i+1)
	}
	eleven[0] = target.ID
	cases := []struct {
		name   string
		action string
		ids    []string
	}{
		{"eleven ids", "disable", eleven},
		{"duplicate ids", "disable", []string{target.ID, target.ID}},
		{"empty ids", "disable", []string{}},
		{"unknown action", "freeze", []string{target.ID}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			resp, payload := userBatchDo(t, e, e.client, masterSession.CSRFToken, "master-pass",
				tc.action, tc.ids)
			if resp.StatusCode != http.StatusBadRequest {
				t.Fatalf("%s = %d, want 400", tc.name, resp.StatusCode)
			}
			if code, _ := errorCode(t, payload); code != "validation_failed" {
				t.Fatalf("%s code = %q, want validation_failed", tc.name, code)
			}
		})
	}
	// 校验拒绝零副作用：目标用户仍是 active，名下零审计行，
	// 整个 schema 没有写出任何用户审计行。
	if got := userBatchStatus(t, e, target.ID, false); got != "active" {
		t.Fatalf("target status after rejections = %q, want active", got)
	}
	if got := userBatchAuditCount(t, e, target.ID); got != 0 {
		t.Fatalf("audit rows for the target after rejections = %d, want 0", got)
	}
	if got := e.scalarInt("SELECT count(*) FROM audit_logs WHERE target_type = 'user'"); got != 0 {
		t.Fatalf("user audit rows after rejections = %d, want 0", got)
	}
}

// 验收⑥：批量软删含自删项。自删 per-item 403，同批其余条目继续成功。
func TestUserBatchDeleteSelfForbidden(t *testing.T) {
	e := newAuthEnv(t)

	u2SeedUser(t, e, "ubatch-self-master@example.com", "master-pass", coreauth.RoleMaster)
	masterSession := u2LoginOn(e, e.client, "ubatch-self-master@example.com", "master-pass")
	other := w2dSeedUser(t, e, "ubatch-self-other@example.com", "user-pass", coreauth.RoleUser)

	resp, payload := userBatchDo(t, e, e.client, masterSession.CSRFToken, "master-pass",
		"delete", []string{masterSession.ID, other.ID})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("self-delete batch = %d, want 200", resp.StatusCode)
	}
	result := userBatchDecode(t, payload)
	if len(result.Items) != 2 {
		t.Fatalf("items = %d, want 2", len(result.Items))
	}
	if result.Items[0].ID != masterSession.ID || result.Items[0].Status != "error" ||
		result.Items[0].Code == nil || *result.Items[0].Code != "forbidden" {
		t.Fatalf("items[0] = %s, want error forbidden for the self-delete item", userBatchItemShape(result.Items[0]))
	}
	if result.Items[1].ID != other.ID || result.Items[1].Status != "ok" {
		t.Fatalf("items[1] = %s, want ok after the failure", userBatchItemShape(result.Items[1]))
	}
	if got := userBatchStatus(t, e, masterSession.ID, false); got != "active" {
		t.Fatalf("master status = %q, want untouched active", got)
	}
	if got := userBatchAuditCount(t, e, masterSession.ID); got != 0 {
		t.Fatalf("audit rows for the self-delete item = %d, want 0", got)
	}
	if got := userBatchAuditAction(t, e, other.ID, "user.delete"); got != 1 {
		t.Fatalf("delete audit rows for %s = %d, want 1", other.ID, got)
	}
}

// 验收⑦：恢复撞活跃邮箱 per-item conflict，同批其余条目继续成功。
func TestUserBatchRestoreEmailConflict(t *testing.T) {
	e := newAuthEnv(t)

	u2SeedUser(t, e, "ubatch-restore-master@example.com", "master-pass", coreauth.RoleMaster)
	masterSession := u2LoginOn(e, e.client, "ubatch-restore-master@example.com", "master-pass")
	clashed := w2dSeedUser(t, e, "ubatch-restore-clash@example.com", "user-pass", coreauth.RoleUser)
	smooth := w2dSeedUser(t, e, "ubatch-restore-smooth@example.com", "user-pass", coreauth.RoleUser)

	// 软删两个目标：一个随后被同邮箱新用户占位，一个独占邮箱。
	for _, id := range []string{clashed.ID, smooth.ID} {
		delResp, _ := u2Do(e, e.client, http.MethodDelete,
			"/api/users/"+id, u1ReauthBody("master-pass"), masterSession.CSRFToken)
		if delResp.StatusCode != http.StatusNoContent {
			t.Fatalf("seed delete %s = %d, want 204", id, delResp.StatusCode)
		}
	}
	taker := w2dSeedUser(t, e, "ubatch-restore-clash@example.com", "user-pass", coreauth.RoleUser)
	if taker.ID == clashed.ID {
		t.Fatal("the replacement user must be a distinct row")
	}

	resp, payload := userBatchDo(t, e, e.client, masterSession.CSRFToken, "master-pass",
		"restore", []string{clashed.ID, smooth.ID})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("restore batch = %d, want 200", resp.StatusCode)
	}
	result := userBatchDecode(t, payload)
	if len(result.Items) != 2 {
		t.Fatalf("items = %d, want 2", len(result.Items))
	}
	if result.Items[0].ID != clashed.ID || result.Items[0].Status != "error" ||
		result.Items[0].Code == nil || *result.Items[0].Code != "conflict" {
		t.Fatalf("items[0] = %s, want error conflict for the email clash", userBatchItemShape(result.Items[0]))
	}
	if result.Items[1].ID != smooth.ID || result.Items[1].Status != "ok" {
		t.Fatalf("items[1] = %s, want ok after the failure", userBatchItemShape(result.Items[1]))
	}
	if got := userBatchStatus(t, e, clashed.ID, true); got == "" {
		t.Fatalf("the clashed user %s must stay soft-deleted", clashed.ID)
	}
	if got := userBatchStatus(t, e, smooth.ID, false); got == "" {
		t.Fatalf("the restored user %s has no live row", smooth.ID)
	}
	if got := userBatchAuditAction(t, e, clashed.ID, "user.restore"); got != 0 {
		t.Fatalf("restore audit rows for the clashed user = %d, want 0", got)
	}
	if got := userBatchAuditAction(t, e, smooth.ID, "user.restore"); got != 1 {
		t.Fatalf("restore audit rows for %s = %d, want 1", smooth.ID, got)
	}
}
