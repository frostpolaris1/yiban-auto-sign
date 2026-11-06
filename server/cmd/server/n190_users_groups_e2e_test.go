package main

// 用户分组与计数 e2e：GET /api/users 的 group 与 counts。
//
// # 功能
// 钉住五条用户可见的性质：
//   1. 每行带名下未软删账号的 account/pending/review 计数。
//   2. group 按账号派生：pending 看 review、normal 看无 review 且有账号、
//      vacant 看没有账号；软删账号不计入。
//   3. counts 是全部用户的全局计数，不随 q/分页收窄；deleted 只数软删用户。
//   4. group 与 include_deleted 互斥，组合请求 400。
//   5. 变更接口（PATCH、restore）的响应同样带名下账号计数：
//      行扫描不含聚合列，用例层写成功后补齐。
//
// # 归属
// cmd/server 可同时引用 adapters 与 entry；本文件用真实 PostgreSQL。
//
// # 口径
// 缺 DSN 时 newAuthEnv 打可见跳过横幅，跳过不是绿。
// 账号直接写库造前置状态；手机号只取登记段。

import (
	"encoding/json"
	"net/http"
	"testing"

	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
)

type n190User struct {
	ID           string `json:"id"`
	Email        string `json:"email"`
	AccountCount int    `json:"account_count"`
	PendingCount int    `json:"pending_count"`
	ReviewCount  int    `json:"review_count"`
	DeletedAt    any    `json:"deleted_at"`
}

type n190Counts struct {
	Pending int `json:"pending"`
	Normal  int `json:"normal"`
	Vacant  int `json:"vacant"`
	Deleted int `json:"deleted"`
}

type n190UserPage struct {
	Items      []n190User `json:"items"`
	NextCursor string     `json:"next_cursor"`
	Counts     n190Counts `json:"counts"`
}

func n190Users(t *testing.T, e *authEnv, path string) n190UserPage {
	t.Helper()
	resp, payload := e.ulGet(path)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("GET %s = %d; body=%q", path, resp.StatusCode, payload)
	}
	var page n190UserPage
	if err := json.Unmarshal(payload, &page); err != nil {
		t.Fatalf("decode %s: %v; body=%q", path, err, payload)
	}
	return page
}

func TestN190UserGroupCountsAndFilters(t *testing.T) {
	e := newAuthEnv(t)
	master := w2dSeedUser(t, e, "n190-groups-boss@example.com", "boss-pass", coreauth.RoleMaster)
	pendingUser := w2dSeedUser(t, e, "n190-pending@example.com", "user-pass", coreauth.RoleUser)
	normalUser := w2dSeedUser(t, e, "n190-normal@example.com", "user-pass", coreauth.RoleUser)
	deletedAccountUser := w2dSeedUser(t, e, "n190-deleted-account@example.com", "user-pass", coreauth.RoleUser)
	vacantUser := w2dSeedUser(t, e, "n190-vacant@example.com", "user-pass", coreauth.RoleUser)
	deletedUser := w2dSeedUser(t, e, "n190-deleted-user@example.com", "user-pass", coreauth.RoleUser)
	if err := e.users.SoftDelete(e.ctx, deletedUser.ID, master.ID, clock.System{}.Now().UTC()); err != nil {
		t.Fatalf("soft delete user: %v", err)
	}

	schoolID, err := e.db.QueryString(e.ctx,
		`INSERT INTO schools (code, name) VALUES ('n190-groups', 'N190 Groups') RETURNING id::text`)
	if err != nil {
		t.Fatalf("seed school: %v", err)
	}
	seedAccount := func(owner, phone string, status coreaccount.Status, reason string) {
		t.Helper()
		if _, err := e.db.Exec(e.ctx, `
			INSERT INTO accounts (school_id, owner_user_id, phone, status, reject_reason)
			VALUES ($1::uuid, $2::uuid, $3, $4::account_status, $5)`,
			schoolID, owner, phone, string(status), reason); err != nil {
			t.Fatalf("seed account %s: %v", phone, err)
		}
	}
	seedAccount(master.ID, "13800000013", coreaccount.StatusActive, "")
	seedAccount(pendingUser.ID, "13800000040", coreaccount.StatusPending, "")
	seedAccount(pendingUser.ID, "13800000041", coreaccount.StatusRejected, "资料不全")
	seedAccount(normalUser.ID, "13800000050", coreaccount.StatusActive, "")
	seedAccount(deletedAccountUser.ID, "13800000060", coreaccount.StatusActive, "")
	seedAccount(deletedAccountUser.ID, "13800000012", coreaccount.StatusActive, "")
	if _, err := e.db.Exec(e.ctx, `
		UPDATE accounts SET deleted_at = now(), deleted_by = $1::uuid WHERE phone = $2`,
		master.ID, "13800000012"); err != nil {
		t.Fatalf("soft delete account: %v", err)
	}

	e.w2dLoginAs("n190-groups-boss@example.com", "boss-pass")
	page := n190Users(t, e, "/api/users")
	want := n190Counts{Pending: 1, Normal: 3, Vacant: 1, Deleted: 1}
	if page.Counts != want {
		t.Fatalf("counts = %+v, want %+v", page.Counts, want)
	}

	byEmail := map[string]n190User{}
	for _, item := range page.Items {
		byEmail[item.Email] = item
	}
	if got := byEmail["n190-pending@example.com"]; got.AccountCount != 2 || got.PendingCount != 1 || got.ReviewCount != 2 {
		t.Fatalf("pending user counts = %+v, want 2/1/2", got)
	}
	if got := byEmail["n190-deleted-account@example.com"]; got.AccountCount != 1 || got.ReviewCount != 0 {
		t.Fatalf("soft-deleted account leaked into counts: %+v", got)
	}
	if got := byEmail["n190-vacant@example.com"]; got.AccountCount != 0 || got.ReviewCount != 0 {
		t.Fatalf("vacant user counts = %+v, want 0/0/0", got)
	}

	// group 只回该组成员；软删账号不影响 normal 归属。
	pendingPage := n190Users(t, e, "/api/users?group=pending")
	if len(pendingPage.Items) != 1 || pendingPage.Items[0].ID != pendingUser.ID {
		t.Fatalf("pending group = %+v, want only %s", pendingPage.Items, pendingUser.Email)
	}
	if pendingPage.Counts != want {
		t.Fatalf("group query narrowed counts: %+v", pendingPage.Counts)
	}
	normalPage := n190Users(t, e, "/api/users?group=normal")
	normalIDs := map[string]bool{}
	for _, item := range normalPage.Items {
		normalIDs[item.ID] = true
	}
	for _, id := range []string{master.ID, normalUser.ID, deletedAccountUser.ID} {
		if !normalIDs[id] {
			t.Fatalf("normal group misses %s: %+v", id, normalPage.Items)
		}
	}
	if len(normalPage.Items) != 3 {
		t.Fatalf("normal group = %d rows, want 3", len(normalPage.Items))
	}
	vacantPage := n190Users(t, e, "/api/users?group=vacant")
	if len(vacantPage.Items) != 1 || vacantPage.Items[0].ID != vacantUser.ID {
		t.Fatalf("vacant group = %+v, want only %s", vacantPage.Items, vacantUser.Email)
	}

	// counts 不随 q 收窄。
	filtered := n190Users(t, e, "/api/users?q=n190-normal")
	if filtered.Counts != want {
		t.Fatalf("q narrowed counts: %+v", filtered.Counts)
	}

	// group 与 include_deleted 互斥；未知分组显性 400。
	for _, path := range []string{
		"/api/users?group=pending&include_deleted=true",
		"/api/users?group=bogus",
	} {
		resp, payload := e.ulGet(path)
		if resp.StatusCode != http.StatusBadRequest {
			t.Fatalf("GET %s = %d, want 400; body=%q", path, resp.StatusCode, payload)
		}
		if code, _ := errorCode(t, payload); code != "validation_failed" {
			t.Fatalf("GET %s code = %q, want validation_failed", path, code)
		}
	}
}

// TestN190MutatingEndpointsCarryCounts：变更接口的响应也带名下账号计数。
// PATCH 与 Restore 的行扫描不含聚合列，用例层在写成功后补齐；
// 断言响应计数与列表行同形状且非零。
func TestN190MutatingEndpointsCarryCounts(t *testing.T) {
	e := newAuthEnv(t)
	boss := w2dSeedUser(t, e, "n190-mut-boss@example.com", "boss-pass", coreauth.RoleMaster)
	session := e.w2dLoginAs("n190-mut-boss@example.com", "boss-pass")

	schoolID, err := e.db.QueryString(e.ctx,
		`INSERT INTO schools (code, name) VALUES ('n190-mut', 'N190 Mut') RETURNING id::text`)
	if err != nil {
		t.Fatalf("seed school: %v", err)
	}
	seedAccount := func(owner, phone string, status coreaccount.Status, reason string) {
		t.Helper()
		if _, err := e.db.Exec(e.ctx, `
			INSERT INTO accounts (school_id, owner_user_id, phone, status, reject_reason)
			VALUES ($1::uuid, $2::uuid, $3, $4::account_status, $5)`,
			schoolID, owner, phone, string(status), reason); err != nil {
			t.Fatalf("seed account %s: %v", phone, err)
		}
	}

	// PATCH：有账号的用户改状态，响应计数非零且与列表行同一口径。
	// active+pending+rejected 三态齐全，钉住 review 含 pending 与 rejected。
	target := w2dSeedUser(t, e, "n190-mut-target@example.com", "user-pass", coreauth.RoleUser)
	seedAccount(target.ID, "13800000070", coreaccount.StatusActive, "")
	seedAccount(target.ID, "13800000071", coreaccount.StatusPending, "")
	seedAccount(target.ID, "13800000073", coreaccount.StatusRejected, "资料不全")

	resp, payload := e.do(http.MethodPatch, "/api/users/"+target.ID,
		map[string]any{"status": "disabled", "current_password": "boss-pass"},
		map[string]string{"X-CSRF-Token": session.CSRFToken})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("PATCH = %d; body=%q", resp.StatusCode, payload)
	}
	var patched n190User
	if err := json.Unmarshal(payload, &patched); err != nil {
		t.Fatalf("decode patch: %v; body=%q", err, payload)
	}
	if patched.AccountCount != 3 || patched.PendingCount != 1 || patched.ReviewCount != 2 {
		t.Fatalf("patched counts = %+v, want 3/1/2", patched)
	}

	// Restore：软删用户名下有账号，恢复响应计数非零。
	deleted := u1SeedDeletedUser(t, e, "n190-mut-deleted@example.com", boss.ID)
	seedAccount(deleted.ID, "13800000072", coreaccount.StatusActive, "")

	resp, payload = e.do(http.MethodPost, "/api/users/"+deleted.ID+"/restore",
		u1ReauthBody("boss-pass"),
		map[string]string{"X-CSRF-Token": session.CSRFToken})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("restore = %d; body=%q", resp.StatusCode, payload)
	}
	var back n190User
	if err := json.Unmarshal(payload, &back); err != nil {
		t.Fatalf("decode restore: %v; body=%q", err, payload)
	}
	if back.AccountCount != 1 || back.PendingCount != 0 || back.ReviewCount != 0 {
		t.Fatalf("restored counts = %+v, want 1/0/0", back)
	}
}
