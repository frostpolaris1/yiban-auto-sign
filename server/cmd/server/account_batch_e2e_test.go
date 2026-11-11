package main

// 账号批量动作端点的端到端测试。真库、真 HTTP、真 cookie。
//
// # 功能
// 钉住 POST /api/accounts/batch 的验收链：master 全 ok 批量且逐条审计、
// 混合成败按序给出 per-item 稳定码、admin 只对绑定校成功且跨校项零审计、
// 二次口令失败零写入、请求级校验 400、驳回理由进审计 detail。
// 本文件**不**复制入口层替身单测。
//
// # 归属
// cmd/server 是唯一可同时引用 adapters、app、entry 的位置。
// 环境复用 auth_e2e_test.go 的 newAuthEnv 与 ux2 的种子帮手。
//
// # 口径
// 缺 DSN 时整包跳过并打可见横幅。跳过不是绿。
// 每个测试独占一个 schema，互不依赖。
// 手机号只取登记的合成段（tests/synthetic-fixtures.json）。
// 审计计数一律按 target_id：成功项各一条、失败项零条。

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"testing"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

// 本文件的合成手机号全部取自登记段（tests/synthetic-fixtures.json）。
const (
	batchPhonePendingA = "13900000011"
	batchPhonePendingB = "13900000012"
	batchPhoneActive   = "13900000013"
	batchPhoneDeleted  = "13900000014"
	batchPhoneLive     = "13800138098"
	batchPhoneGone     = "13800138099"
	batchPhoneBound    = "13800138101"
	batchPhoneForeign  = "13800138102"
	batchPhoneReject   = "13900000041"
)

// batchMissingID 是一个形状合法但不存在的账号 id（零前缀合成段）。
const batchMissingID = "00000000-0000-0000-0000-0000000000ab"

// batchItem 与 batchResult 是批量响应里用到的字段。
type batchItem struct {
	ID      string  `json:"id"`
	Status  string  `json:"status"`
	Code    *string `json:"code"`
	Message *string `json:"message"`
}

type batchResult struct {
	Items []batchItem `json:"items"`
}

// batchDo 以指定客户端发一次批量请求并返回状态码与响应体。
func batchDo(t *testing.T, e *authEnv, client *http.Client, csrf, password, action string, ids []string, reason string) (*http.Response, []byte) {
	t.Helper()
	body := map[string]any{"action": action, "ids": ids, "current_password": password}
	if reason != "" {
		body["reason"] = reason
	}
	return u2Do(e, client, http.MethodPost, "/api/accounts/batch", body, csrf)
}

// batchDecode 解码批量响应，失败即终止。
func batchDecode(t *testing.T, payload []byte) batchResult {
	t.Helper()
	var out batchResult
	if err := json.Unmarshal(payload, &out); err != nil {
		t.Fatalf("decode batch result: %v; body=%q", err, payload)
	}
	return out
}

// batchSeedAccount 以 master 代建一个 pending 账号并返回 id。
// 一人一号：批量面一次要操作多行账号，每行各配一个新属主。
func batchSeedAccount(t *testing.T, e *authEnv, session sessionUserBody, schoolID, phone string) string {
	t.Helper()
	owner := u2SeedUser(t, e, "batch-owner-"+randomHex(4)+"@example.com", "batch-pass-2", coreauth.RoleUser)
	resp, payload := u2CreateAccount(e, e.client, session.CSRFToken, u2CreateBody{
		SchoolID: schoolID, Phone: phone, Password: "batch-pass-2", OwnerUserID: owner.ID,
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

// batchApprove 先用单条审核端点把账号推到 active。
func batchApprove(t *testing.T, e *authEnv, session sessionUserBody, id string) {
	t.Helper()
	resp, payload := u2Do(e, e.client, http.MethodPost, "/api/accounts/"+id+"/review",
		map[string]string{"decision": "approve"}, session.CSRFToken)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("approve %s = %d, want 200; body=%q", id, resp.StatusCode, payload)
	}
}

// batchAuditCount 数某 target 的全部审计行。
func batchAuditCount(t *testing.T, e *authEnv, targetID string) int64 {
	t.Helper()
	return e.scalarInt("SELECT count(*) FROM audit_logs WHERE target_id = $1", targetID)
}

// batchAuditAction 数某 target 的指定动作审计行。
func batchAuditAction(t *testing.T, e *authEnv, targetID, action string) int64 {
	t.Helper()
	return e.scalarInt(
		"SELECT count(*) FROM audit_logs WHERE target_id = $1 AND action = $2", targetID, action)
}

// batchStatus 读账号的库内状态；deletedAt 为真时读软删行，否则读活行。
func batchStatus(t *testing.T, e *authEnv, id string, deletedAt bool) string {
	t.Helper()
	sql := "SELECT coalesce(status::text, '') FROM accounts WHERE id = $1"
	if deletedAt {
		sql += " AND deleted_at IS NOT NULL"
	} else {
		sql += " AND deleted_at IS NULL"
	}
	return e.scalarString(sql, id)
}

// 验收①：master 批量审核通过两个 pending、暂停一个 active、软删一个
// active。全部 ok，审计逐条存在，库内状态逐条落位。
func TestAccountBatchMasterAllOk(t *testing.T) {
	e := newAuthEnv(t)
	schoolID := u2SeedSchool(t, e, "batch-a")

	u2SeedUser(t, e, "batch-master@example.com", "master-pass", coreauth.RoleMaster)
	masterSession := u2LoginOn(e, e.client, "batch-master@example.com", "master-pass")

	pendingA := batchSeedAccount(t, e, masterSession, schoolID, batchPhonePendingA)
	pendingB := batchSeedAccount(t, e, masterSession, schoolID, batchPhonePendingB)
	toPause := batchSeedAccount(t, e, masterSession, schoolID, batchPhoneActive)
	toDelete := batchSeedAccount(t, e, masterSession, schoolID, batchPhoneDeleted)
	batchApprove(t, e, masterSession, toPause)
	batchApprove(t, e, masterSession, toDelete)

	// 批量审核通过：两个 pending 一起转 active。
	resp, payload := batchDo(t, e, e.client, masterSession.CSRFToken, "master-pass",
		"review_approve", []string{pendingA, pendingB}, "")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("batch approve = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	result := batchDecode(t, payload)
	if len(result.Items) != 2 {
		t.Fatalf("approve items = %+v, want 2", result.Items)
	}
	for i, id := range []string{pendingA, pendingB} {
		item := result.Items[i]
		if item.ID != id || item.Status != "ok" || item.Code != nil {
			t.Fatalf("approve items[%d] = %+v, want ok for %s", i, item, id)
		}
		if got := batchStatus(t, e, id, false); got != "active" {
			t.Fatalf("status of %s = %q, want active", id, got)
		}
		if got := batchAuditAction(t, e, id, "account.review_approve"); got != 1 {
			t.Fatalf("approve audit rows for %s = %d, want 1", id, got)
		}
	}

	// 批量暂停：active 转 paused_admin。
	resp, payload = batchDo(t, e, e.client, masterSession.CSRFToken, "master-pass",
		"pause", []string{toPause}, "")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("batch pause = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	result = batchDecode(t, payload)
	if len(result.Items) != 1 || result.Items[0].ID != toPause || result.Items[0].Status != "ok" {
		t.Fatalf("pause items = %+v, want one ok for %s", result.Items, toPause)
	}
	if got := batchStatus(t, e, toPause, false); got != "paused_admin" {
		t.Fatalf("status of %s = %q, want paused_admin", toPause, got)
	}
	if got := batchAuditAction(t, e, toPause, "account.pause"); got != 1 {
		t.Fatalf("pause audit rows for %s = %d, want 1", toPause, got)
	}

	// 批量软删：deleted_at 落库，读立即 404 的行由状态口径覆盖。
	resp, payload = batchDo(t, e, e.client, masterSession.CSRFToken, "master-pass",
		"delete", []string{toDelete}, "")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("batch delete = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	result = batchDecode(t, payload)
	if len(result.Items) != 1 || result.Items[0].ID != toDelete || result.Items[0].Status != "ok" {
		t.Fatalf("delete items = %+v, want one ok for %s", result.Items, toDelete)
	}
	if got := batchStatus(t, e, toDelete, true); got == "" {
		t.Fatalf("the deleted account %s has no soft-deleted row", toDelete)
	}
	if got := batchAuditAction(t, e, toDelete, "account.delete"); got != 1 {
		t.Fatalf("delete audit rows for %s = %d, want 1", toDelete, got)
	}
}

// 验收②：混合成败按序返回。restore 三个 id：不存在 → not_found，
// 活账号 → conflict，已软删 → ok；HTTP 200，失败项零审计。
func TestAccountBatchMixedPerItem(t *testing.T) {
	e := newAuthEnv(t)
	schoolID := u2SeedSchool(t, e, "batch-b")

	u2SeedUser(t, e, "batch-mix-master@example.com", "master-pass", coreauth.RoleMaster)
	masterSession := u2LoginOn(e, e.client, "batch-mix-master@example.com", "master-pass")

	live := batchSeedAccount(t, e, masterSession, schoolID, batchPhoneLive)
	batchApprove(t, e, masterSession, live)
	gone := batchSeedAccount(t, e, masterSession, schoolID, batchPhoneGone)
	batchApprove(t, e, masterSession, gone)

	// 软删一行，供恢复成功项使用。
	delResp, delPayload := u2Do(e, e.client, http.MethodDelete,
		"/api/accounts/"+gone, u1ReauthBody("master-pass"), masterSession.CSRFToken)
	if delResp.StatusCode != http.StatusNoContent {
		t.Fatalf("seed delete = %d, want 204; body=%q", delResp.StatusCode, delPayload)
	}
	if got := batchAuditAction(t, e, gone, "account.review_approve"); got != 1 {
		t.Fatalf("review audit rows for %s = %d, want 1", gone, got)
	}
	if got := batchAuditAction(t, e, gone, "account.delete"); got != 1 {
		t.Fatalf("delete audit rows for %s = %d, want 1", gone, got)
	}

	resp, payload := batchDo(t, e, e.client, masterSession.CSRFToken, "master-pass",
		"restore", []string{batchMissingID, live, gone}, "")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("mixed restore = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	result := batchDecode(t, payload)
	if len(result.Items) != 3 {
		t.Fatalf("mixed items = %+v, want 3", result.Items)
	}

	first := result.Items[0]
	if first.ID != batchMissingID || first.Status != "error" || first.Code == nil || *first.Code != "not_found" {
		t.Fatalf("items[0] = %+v, want error not_found", first)
	}
	second := result.Items[1]
	if second.ID != live || second.Status != "error" || second.Code == nil || *second.Code != "conflict" {
		t.Fatalf("items[1] = %+v, want error conflict", second)
	}
	third := result.Items[2]
	if third.ID != gone || third.Status != "ok" || third.Code != nil {
		t.Fatalf("items[2] = %+v, want ok", third)
	}

	// 失败项零审计：不存在 id 无行，活账号的恢复尝试无新行。
	if got := batchAuditCount(t, e, batchMissingID); got != 0 {
		t.Fatalf("audit rows for the missing id = %d, want 0", got)
	}
	if got := batchAuditAction(t, e, live, "account.restore"); got != 0 {
		t.Fatalf("restore audit rows for the live account = %d, want 0", got)
	}
	// 成功项恰好一条恢复审计，行回到活表并保留删除前状态。
	if got := batchAuditAction(t, e, gone, "account.restore"); got != 1 {
		t.Fatalf("restore audit rows for %s = %d, want 1", gone, got)
	}
	if got := batchStatus(t, e, gone, false); got != "active" {
		t.Fatalf("restored status of %s = %q, want the pre-delete active", gone, got)
	}
}

// 验收③：admin 只对绑定校成功；跨校项 per-item 失败且零审计。
func TestAccountBatchAdminScopePerItem(t *testing.T) {
	e := newAuthEnv(t)
	schoolA := u2SeedSchool(t, e, "batch-c-a")
	schoolB := u2SeedSchool(t, e, "batch-c-b")

	u2SeedUser(t, e, "batch-admin-master@example.com", "master-pass", coreauth.RoleMaster)
	admin := u2SeedUser(t, e, "batch-admin@example.com", "admin-pass", coreauth.RoleAdmin)
	u2BindSchool(t, e, admin.ID, schoolA)
	masterSession := u2LoginOn(e, e.client, "batch-admin-master@example.com", "master-pass")
	adminClient := u2FreshClient(t)
	adminSession := u2LoginOn(e, adminClient, "batch-admin@example.com", "admin-pass")

	bound := batchSeedAccount(t, e, masterSession, schoolA, batchPhoneBound)
	foreign := batchSeedAccount(t, e, masterSession, schoolB, batchPhoneForeign)
	batchApprove(t, e, masterSession, bound)
	batchApprove(t, e, masterSession, foreign)

	resp, payload := batchDo(t, e, adminClient, adminSession.CSRFToken, "admin-pass",
		"pause", []string{bound, foreign}, "")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("admin batch = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	result := batchDecode(t, payload)
	if len(result.Items) != 2 {
		t.Fatalf("admin items = %+v, want 2", result.Items)
	}
	if result.Items[0].ID != bound || result.Items[0].Status != "ok" {
		t.Fatalf("items[0] = %+v, want ok for the bound-school account", result.Items[0])
	}
	if result.Items[1].ID != foreign || result.Items[1].Status != "error" ||
		result.Items[1].Code == nil || *result.Items[1].Code != "not_found" {
		t.Fatalf("items[1] = %+v, want error not_found for the cross-school account", result.Items[1])
	}

	if got := batchStatus(t, e, bound, false); got != "paused_admin" {
		t.Fatalf("status of %s = %q, want paused_admin", bound, got)
	}
	if got := batchStatus(t, e, foreign, false); got != "active" {
		t.Fatalf("status of %s = %q, want unchanged active", foreign, got)
	}
	// 跨校项零审计：除建号与审核的种子行外，批量没有为它写任何行。
	if got := batchAuditAction(t, e, foreign, "account.pause"); got != 0 {
		t.Fatalf("pause audit rows for the cross-school account = %d, want 0", got)
	}
	if got := batchAuditAction(t, e, bound, "account.pause"); got != 1 {
		t.Fatalf("pause audit rows for %s = %d, want 1", bound, got)
	}
}

// 验收④：二次口令失败零写入零审计。缺失 400、错误 403。
func TestAccountBatchReauthGate(t *testing.T) {
	e := newAuthEnv(t)
	schoolID := u2SeedSchool(t, e, "batch-d")

	u2SeedUser(t, e, "batch-reauth-master@example.com", "master-pass", coreauth.RoleMaster)
	masterSession := u2LoginOn(e, e.client, "batch-reauth-master@example.com", "master-pass")
	target := batchSeedAccount(t, e, masterSession, schoolID, batchPhoneDeleted)
	batchApprove(t, e, masterSession, target)

	// 缺口令：400，账号未动，零审计（审核行除外的前置基线单独数）。
	before := batchAuditCount(t, e, target)
	resp, payload := batchDo(t, e, e.client, masterSession.CSRFToken, "",
		"pause", []string{target}, "")
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("missing password = %d, want 400; body=%q", resp.StatusCode, payload)
	}
	if code, _ := errorCode(t, payload); code != "validation_failed" {
		t.Fatalf("missing password code = %q, want validation_failed", code)
	}

	// 错误口令：403 forbidden 信封。
	resp, payload = batchDo(t, e, e.client, masterSession.CSRFToken, "wrong-pass-42",
		"pause", []string{target}, "")
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("wrong password = %d, want 403; body=%q", resp.StatusCode, payload)
	}
	if code, _ := errorCode(t, payload); code != "forbidden" {
		t.Fatalf("wrong password code = %q, want forbidden", code)
	}

	// 两次失败都零写入：状态不变、无新增审计。
	if got := batchStatus(t, e, target, false); got != "active" {
		t.Fatalf("status after failures = %q, want active", got)
	}
	if got := batchAuditCount(t, e, target); got != before {
		t.Fatalf("audit rows after failures = %d, want %d", got, before)
	}
}

// 验收⑤：请求级校验整批拒绝。11 个 id、重复 id、空 ids、未知动作都
// 400 validation_failed；失败零审计。
func TestAccountBatchValidationErrors(t *testing.T) {
	e := newAuthEnv(t)
	u2SeedUser(t, e, "batch-validate-master@example.com", "master-pass", coreauth.RoleMaster)
	masterSession := u2LoginOn(e, e.client, "batch-validate-master@example.com", "master-pass")

	// 11 个互不相同的合法形状 id：命中的只能是 >10，
	// 不会先撞上重复判据，让「超限」与「重复」两个判据各自可辨。
	eleven := make([]string, 11)
	for i := range eleven {
		eleven[i] = fmt.Sprintf("00000000-0000-0000-0000-%012d", i+1)
	}
	cases := []struct {
		name   string
		action string
		ids    []string
	}{
		{"eleven ids", "pause", eleven},
		{"duplicate ids", "pause", []string{batchMissingID, batchMissingID}},
		{"case-variant duplicate", "pause", []string{batchMissingID, strings.ToUpper(batchMissingID)}},
		{"empty ids", "pause", []string{}},
		{"unknown action", "freeze", []string{batchMissingID}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			resp, payload := batchDo(t, e, e.client, masterSession.CSRFToken, "master-pass",
				tc.action, tc.ids, "")
			if resp.StatusCode != http.StatusBadRequest {
				t.Fatalf("%s = %d, want 400; body=%q", tc.name, resp.StatusCode, payload)
			}
			if code, _ := errorCode(t, payload); code != "validation_failed" {
				t.Fatalf("%s code = %q, want validation_failed", tc.name, code)
			}
		})
	}
	// 校验拒绝零副作用：整个 schema 没有写出任何账号审计行。
	if got := e.scalarInt("SELECT count(*) FROM audit_logs WHERE target_type = 'account'"); got != 0 {
		t.Fatalf("account audit rows after rejections = %d, want 0", got)
	}
}

// 验收⑥：review_reject 的 reason 进入审计 detail（按目标账号查
// account.review_reject 行并断言理由原文在内）。
func TestAccountBatchRejectReasonAudited(t *testing.T) {
	e := newAuthEnv(t)
	schoolID := u2SeedSchool(t, e, "batch-e")

	u2SeedUser(t, e, "batch-reject-master@example.com", "master-pass", coreauth.RoleMaster)
	masterSession := u2LoginOn(e, e.client, "batch-reject-master@example.com", "master-pass")
	target := batchSeedAccount(t, e, masterSession, schoolID, batchPhoneReject)

	// 纯中文理由不含号码与邮箱形态，掩码口径原样保留，可按原文断言。
	const reason = "批量驳回合成理由"
	resp, payload := batchDo(t, e, e.client, masterSession.CSRFToken, "master-pass",
		"review_reject", []string{target}, reason)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("batch reject = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	result := batchDecode(t, payload)
	if len(result.Items) != 1 || result.Items[0].ID != target || result.Items[0].Status != "ok" {
		t.Fatalf("reject items = %+v, want one ok for %s", result.Items, target)
	}
	if got := batchStatus(t, e, target, false); got != "rejected" {
		t.Fatalf("status of %s = %q, want rejected", target, got)
	}
	if got := batchAuditAction(t, e, target, "account.review_reject"); got != 1 {
		t.Fatalf("review_reject audit rows for %s = %d, want 1", target, got)
	}
	detail := e.scalarString(
		"SELECT detail FROM audit_logs WHERE target_id = $1 AND action = 'account.review_reject'",
		target)
	if !strings.Contains(detail, reason) {
		t.Fatalf("review_reject detail = %q, want it to carry the reason %q", detail, reason)
	}
}
