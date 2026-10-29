package main

// 用户列表 e2e：GET /api/users（仅 master）。
//
// # 功能
// 覆盖用户管理页的读取面：角色门槛、游标分页、过滤、软删与响应形状。
// 走真实栈：真库、真登录、真 cookie、真 HTTP。
// 本文件**不**测性能，**不**复制仓储层用例。
//
// # 归属
// cmd/server 是唯一可同时引用 adapters、app、entry 的位置。
// 环境复用 auth_e2e_test.go 的 newAuthEnv。
//
// # 口径
// 缺 DSN 时整包跳过，并打印横幅。跳过不是绿。
// 断言用户可见结果：状态码、错误码、items 形状、无凭据字段。
// next_cursor 沿用 accounts/schools 口径：键恒在，末页为空串。

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"sort"
	"strings"
	"testing"
	"time"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// ulUser 是契约 User 形状的响应体。
type ulUser struct {
	ID                 string     `json:"id"`
	Email              string     `json:"email"`
	Role               string     `json:"role"`
	Status             string     `json:"status"`
	MustChangePassword bool       `json:"must_change_password"`
	CreatedAt          time.Time  `json:"created_at"`
	LastLoginAt        *time.Time `json:"last_login_at"`
}

// ulSeed 直接写一行用户。
// 列表用例不登录这些账号，故哈希用固定占位串，省掉 argon2 的开销。
func ulSeed(t *testing.T, e *authEnv, email string, role coreauth.Role, status coreauth.Status) coreuser.Record {
	t.Helper()
	var rec coreuser.Record
	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		var err error
		rec, err = e.users.Create(ctx, coreuser.New{
			Email:        email,
			PasswordHash: "placeholder-not-a-real-hash",
			Role:         role,
		}, 10000)
		return err
	})
	if err != nil {
		t.Fatalf("seed user %s: %v", email, err)
	}
	if status != "" && status != coreauth.StatusActive {
		if err := e.users.SetStatus(e.ctx, rec.ID, status, clock.System{}.Now().UTC()); err != nil {
			t.Fatalf("set status of %s: %v", email, err)
		}
		rec.Status = status
	}
	return rec
}

// ulGet 以当前会话读一页用户。
func (e *authEnv) ulGet(path string) (*http.Response, []byte) {
	e.t.Helper()
	return e.do(http.MethodGet, path, nil, nil)
}

// ulPage 读一页并解码。状态码不是 200 即终止。
func (e *authEnv) ulPage(path string) (ulPageBody, []byte) {
	e.t.Helper()
	resp, payload := e.ulGet(path)
	if resp.StatusCode != http.StatusOK {
		e.t.Fatalf("GET %s = %d, want 200; body=%q", path, resp.StatusCode, payload)
	}
	var page ulPageBody
	if err := json.Unmarshal(payload, &page); err != nil {
		e.t.Fatalf("decode page: %v; body=%q", err, payload)
	}
	return page, payload
}

// ulPageBody 是列表响应。NextCursor 用指针区分「键缺失」与「空串」。
type ulPageBody struct {
	Items      []ulUser `json:"items"`
	NextCursor *string  `json:"next_cursor"`
}

func ulIDs(page ulPageBody) []string {
	out := make([]string, 0, len(page.Items))
	for _, item := range page.Items {
		out = append(out, item.ID)
	}
	return out
}

// --- 角色门槛 --------------------------------------------------------------

// 模拟真实用户打开用户管理页。
// 断言用户可见结果：master 读到列表；admin/user 403；无会话 401。
func TestUsersListMasterOnly(t *testing.T) {
	e := newAuthEnv(t)
	w2dSeedUser(t, e, "ul-boss@example.com", "boss-pass", coreauth.RoleMaster)
	w2dSeedUser(t, e, "ul-admin@example.com", "admin-pass", coreauth.RoleAdmin)
	w2dSeedUser(t, e, "ul-user@example.com", "user-pass", coreauth.RoleUser)

	// 无会话：独立客户端，不带任何 cookie。
	req, err := http.NewRequestWithContext(e.ctx, http.MethodGet, e.baseURL+"/api/users", nil)
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	resp, err := (&http.Client{Timeout: 10 * time.Second}).Do(req)
	if err != nil {
		t.Fatalf("GET /api/users without a session: %v", err)
	}
	payload, err := io.ReadAll(resp.Body)
	resp.Body.Close()
	if err != nil {
		t.Fatalf("read body: %v", err)
	}
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("anonymous status = %d, want 401; body=%q", resp.StatusCode, payload)
	}
	if code, _ := errorCode(t, payload); code != "unauthorized" {
		t.Fatalf("anonymous code = %q, want unauthorized", code)
	}

	for _, tc := range []struct {
		email    string
		password string
		want     int
	}{
		{"ul-admin@example.com", "admin-pass", http.StatusForbidden},
		{"ul-user@example.com", "user-pass", http.StatusForbidden},
		{"ul-boss@example.com", "boss-pass", http.StatusOK},
	} {
		e.w2dLoginAs(tc.email, tc.password)
		resp, payload := e.ulGet("/api/users")
		if resp.StatusCode != tc.want {
			t.Fatalf("%s GET /api/users = %d, want %d; body=%q",
				tc.email, resp.StatusCode, tc.want, payload)
		}
		if tc.want == http.StatusForbidden {
			if code, _ := errorCode(t, payload); code != "forbidden" {
				t.Fatalf("%s code = %q, want forbidden", tc.email, code)
			}
		}
	}
}

// --- 分页 ------------------------------------------------------------------

// 模拟 master 逐页翻完用户管理页。
// 断言用户可见结果：两页拼接无重无漏；末页 next_cursor 是空串且键存在。
func TestUsersListPaginatesWithoutDuplicatesOrGaps(t *testing.T) {
	e := newAuthEnv(t)
	master := w2dSeedUser(t, e, "page-boss@example.com", "boss-pass", coreauth.RoleMaster)
	want := map[string]bool{master.ID: true}
	for i := 0; i < 5; i++ {
		rec := ulSeed(t, e, "page-"+string(rune('a'+i))+"@example.com", coreauth.RoleUser, coreauth.StatusActive)
		want[rec.ID] = true
	}
	e.w2dLoginAs("page-boss@example.com", "boss-pass")

	var got []string
	cursor := ""
	for page := 0; page < 4; page++ {
		path := "/api/users?limit=2"
		if cursor != "" {
			path += "&cursor=" + cursor
		}
		body, payload := e.ulPage(path)
		if body.NextCursor == nil {
			t.Fatalf("page %d has no next_cursor key; body=%q", page+1, payload)
		}
		if len(body.Items) != 2 {
			t.Fatalf("page %d size = %d, want 2; body=%q", page+1, len(body.Items), payload)
		}
		got = append(got, ulIDs(body)...)
		cursor = *body.NextCursor
		if cursor == "" {
			break
		}
		if page == 2 {
			t.Fatalf("third page still has next_cursor %q; the list must end", cursor)
		}
	}

	if cursor != "" {
		t.Fatalf("next_cursor = %q after the last page, want the empty string", cursor)
	}
	if len(got) != len(want) {
		t.Fatalf("collected %d rows, want %d: %v", len(got), len(want), got)
	}
	seen := map[string]bool{}
	for _, id := range got {
		if seen[id] {
			t.Fatalf("id %s appears twice across pages: %v", id, got)
		}
		seen[id] = true
		if !want[id] {
			t.Fatalf("unexpected id %s in the list", id)
		}
	}
}

// --- 分页：并列 created_at -------------------------------------------------

// 模拟批量导入后 master 逐条翻页。
// 断言用户可见结果：created_at 相同的行也能无重无漏地翻完。
func TestUsersListPaginatesEqualCreatedAt(t *testing.T) {
	e := newAuthEnv(t)
	w2dSeedUser(t, e, "tie-boss@example.com", "boss-pass", coreauth.RoleMaster)
	want := map[string]bool{}
	for i := 0; i < 4; i++ {
		rec := ulSeed(t, e, "tie-"+string(rune('a'+i))+"@example.com", coreauth.RoleUser, coreauth.StatusActive)
		want[rec.ID] = true
		if _, err := e.db.Exec(e.ctx,
			"UPDATE users SET created_at = '2020-01-01T00:00:00Z'::timestamptz WHERE id = $1::uuid",
			rec.ID); err != nil {
			t.Fatalf("pin created_at: %v", err)
		}
	}
	e.w2dLoginAs("tie-boss@example.com", "boss-pass")

	seen := map[string]bool{}
	cursor := ""
	for page := 0; page < 6; page++ {
		path := "/api/users?limit=1"
		if cursor != "" {
			path += "&cursor=" + cursor
		}
		body, payload := e.ulPage(path)
		if len(body.Items) != 1 {
			t.Fatalf("page %d size = %d, want 1; body=%q", page+1, len(body.Items), payload)
		}
		id := body.Items[0].ID
		if seen[id] {
			t.Fatalf("id %s appears twice across pages", id)
		}
		seen[id] = true
		if body.NextCursor == nil {
			t.Fatalf("page %d has no next_cursor key; body=%q", page+1, payload)
		}
		cursor = *body.NextCursor
		if cursor == "" {
			break
		}
	}
	if cursor != "" {
		t.Fatalf("next_cursor = %q after the last page, want the empty string", cursor)
	}
	if len(seen) != len(want)+1 {
		t.Fatalf("collected %d rows, want %d", len(seen), len(want)+1)
	}
	for id := range want {
		if !seen[id] {
			t.Fatalf("tied user %s is missing from the pages", id)
		}
	}
}

// --- 过滤 ------------------------------------------------------------------

// 模拟 master 按角色、状态与邮箱前缀筛选。
// 断言用户可见结果：每个过滤器只命中目标行；非法枚举 400；空结果 items 是 []。
func TestUsersListFiltersRoleStatusAndQuery(t *testing.T) {
	e := newAuthEnv(t)
	w2dSeedUser(t, e, "filter-boss@example.com", "boss-pass", coreauth.RoleMaster)
	admin := ulSeed(t, e, "filter-admin@example.com", coreauth.RoleAdmin, coreauth.StatusActive)
	disabled := ulSeed(t, e, "alice@example.com", coreauth.RoleUser, coreauth.StatusDisabled)
	active := ulSeed(t, e, "alina@example.com", coreauth.RoleUser, coreauth.StatusActive)
	bob := ulSeed(t, e, "bob@example.com", coreauth.RoleUser, coreauth.StatusActive)
	literal := ulSeed(t, e, "a%b@example.com", coreauth.RoleUser, coreauth.StatusActive)
	decoy := ulSeed(t, e, "axb@example.com", coreauth.RoleUser, coreauth.StatusActive)
	e.w2dLoginAs("filter-boss@example.com", "boss-pass")

	cases := []struct {
		name  string
		path  string
		want  []string
		wantK int
	}{
		{"role=admin", "/api/users?role=admin", []string{admin.ID}, 1},
		{"status=disabled", "/api/users?status=disabled", []string{disabled.ID}, 1},
		{"q prefix", "/api/users?q=ali", []string{disabled.ID, active.ID}, 2},
		{"q prefix is case-insensitive", "/api/users?q=ALI", []string{disabled.ID, active.ID}, 2},
		{"q with domain", "/api/users?q=alice@example.com", []string{disabled.ID}, 1},
		{"q treats % literally", "/api/users?q=a%25b", []string{literal.ID}, 1},
		{"role and status", "/api/users?role=user&status=active",
			[]string{active.ID, bob.ID, literal.ID, decoy.ID}, 4},
	}
	for _, tc := range cases {
		page, payload := e.ulPage(tc.path)
		ids := ulIDs(page)
		if len(ids) != tc.wantK {
			t.Fatalf("%s returned %d rows, want %d: %v; body=%q", tc.name, len(ids), tc.wantK, ids, payload)
		}
		sort.Strings(ids)
		sort.Strings(tc.want)
		for i := range ids {
			if ids[i] != tc.want[i] {
				t.Fatalf("%s ids = %v, want %v", tc.name, ids, tc.want)
			}
		}
	}

	// 空结果必须是 []，不是 null。
	_, payload := e.ulPage("/api/users?q=nobody-matches-this")
	if !strings.Contains(string(payload), `"items":[]`) {
		t.Fatalf("empty page body = %q, want items:[]", payload)
	}

	for _, path := range []string{
		"/api/users?role=root",
		"/api/users?status=deleted",
		"/api/users?limit=0",
		"/api/users?limit=201",
		"/api/users?q=" + strings.Repeat("x", 254+1),
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

// --- 软删 ------------------------------------------------------------------

// 模拟 master 删除一个用户后刷新列表。
// 断言用户可见结果：被软删的账号不再出现。
func TestUsersListHidesSoftDeletedUsers(t *testing.T) {
	e := newAuthEnv(t)
	master := w2dSeedUser(t, e, "soft-boss@example.com", "boss-pass", coreauth.RoleMaster)
	victim := ulSeed(t, e, "victim@example.com", coreauth.RoleUser, coreauth.StatusActive)
	e.w2dLoginAs("soft-boss@example.com", "boss-pass")

	page, _ := e.ulPage("/api/users")
	if len(page.Items) != 2 {
		t.Fatalf("before delete: %d rows, want 2", len(page.Items))
	}

	if err := e.users.SoftDelete(e.ctx, victim.ID, master.ID, clock.System{}.Now().UTC()); err != nil {
		t.Fatalf("soft delete victim: %v", err)
	}

	page, payload := e.ulPage("/api/users")
	for _, item := range page.Items {
		if item.ID == victim.ID {
			t.Fatalf("soft-deleted user is still listed; body=%q", payload)
		}
	}
	if len(page.Items) != 1 {
		t.Fatalf("after delete: %d rows, want 1; body=%q", len(page.Items), payload)
	}
}

// --- 响应形状 --------------------------------------------------------------

// 模拟 master 打开列表并检查响应字段。
// 断言用户可见结果：只有契约七个字段；无 password_hash，也无哈希串。
func TestUsersListCarriesNoCredentialFields(t *testing.T) {
	e := newAuthEnv(t)
	w2dSeedUser(t, e, "shape-boss@example.com", "boss-pass", coreauth.RoleMaster)
	ulSeed(t, e, "shape-user@example.com", coreauth.RoleUser, coreauth.StatusActive)
	e.w2dLoginAs("shape-boss@example.com", "boss-pass")

	page, payload := e.ulPage("/api/users")
	if len(page.Items) != 2 {
		t.Fatalf("rows = %d, want 2; body=%q", len(page.Items), payload)
	}

	var rows []map[string]json.RawMessage
	if err := json.Unmarshal(payload, &struct {
		Items *[]map[string]json.RawMessage `json:"items"`
	}{Items: &rows}); err != nil {
		t.Fatalf("decode raw items: %v; body=%q", err, payload)
	}
	want := []string{"created_at", "email", "id", "last_login_at", "must_change_password", "role", "status"}
	for _, row := range rows {
		keys := make([]string, 0, len(row))
		for key := range row {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		if strings.Join(keys, ",") != strings.Join(want, ",") {
			t.Fatalf("user keys = %v, want %v", keys, want)
		}
	}

	for _, forbidden := range []string{"password_hash", "$argon2", "scrypt:"} {
		if strings.Contains(string(payload), forbidden) {
			t.Fatalf("response body contains %q; body=%q", forbidden, payload)
		}
	}
}
