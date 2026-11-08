package main

// 账号与用户列表的条件请求 e2e：GET /api/accounts、GET /api/users。
//
// # 功能
// 钉住列表的 10 秒轮询所依赖的协议面：
//  1. 200 带 ETag，且 ETag 等于响应体的 sha256；
//  2. If-None-Match 命中回 304：无体、保留 ETag、不声明 Content-Type/Content-Length；
//     弱比较、逗号列表、`*` 与重复头行都命中；
//  3. 不同筛选与不同游标得到不同 ETag；
//  4. 写操作后旧 ETag 必须回 200 与新 ETag；
//  5. 鉴权与作用域先于 304：越权请求回 403/只回自己的范围，不能靠旧 ETag 拿 304。
//
// # 归属
// cmd/server 是唯一可同时引用 adapters、app、entry 的位置。
// 环境复用 auth_e2e_test.go 的 newAuthEnv：真库、真登录、真 cookie、真 HTTP。
//
// # 口径
// 缺 DSN 时整包跳过，并打印横幅。跳过不是绿。
// 手机号只取 tests/synthetic-fixtures.json 的登记段；
// 邮箱一律用保留域 example.com，口令明示不是真口令。

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"testing"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	appschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/school"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

// lfAccountPage 是账号列表里断言作用域所需的字段。
type lfAccountPage struct {
	Items []struct {
		ID          string `json:"id"`
		OwnerUserID string `json:"owner_user_id"`
	} `json:"items"`
	NextCursor string `json:"next_cursor"`
}

// lfUserPage 是用户列表里断言可见行所需的字段。
type lfUserPage struct {
	Items []struct {
		ID    string `json:"id"`
		Email string `json:"email"`
	} `json:"items"`
	NextCursor string `json:"next_cursor"`
}

// lfSeedAccount 直接写一行账号，不经建号用例：本文件的 fixtures
// 需要同一属主名下多行 live 状态（列表/清除面不测建号门）。
// 手机号必须是登记过的合成值。
func lfSeedAccount(t *testing.T, e *authEnv, schoolID, ownerID, phone string) string {
	t.Helper()
	id, err := e.db.QueryString(e.ctx, `
		INSERT INTO accounts (school_id, owner_user_id, phone)
		VALUES ($1::uuid, $2::uuid, $3) RETURNING id::text`,
		schoolID, ownerID, phone)
	if err != nil {
		t.Fatalf("seed account %s: %v", phone, err)
	}
	return id
}

// lfSeedSchool 建一所合成学校并返回 ID。
func lfSeedSchool(t *testing.T, e *authEnv, code string) string {
	t.Helper()
	rec, err := pg.NewSchoolRepo(e.db).Create(e.ctx, appschool.New{Code: code, Name: "Freshness " + code})
	if err != nil {
		t.Fatalf("seed school %s: %v", code, err)
	}
	return rec.ID
}

// lfGet 以当前会话读一个路径。inm 的每一项都是一行 If-None-Match：
// 多行必须由入口层合并，而不是让生成绑定判成 400。
func lfGet(e *authEnv, path string, inm []string) (*http.Response, []byte) {
	e.t.Helper()
	req, err := http.NewRequestWithContext(e.ctx, http.MethodGet, e.baseURL+path, nil)
	if err != nil {
		e.t.Fatalf("new request: %v", err)
	}
	for _, value := range inm {
		req.Header.Add("If-None-Match", value)
	}
	resp, err := e.client.Do(req)
	if err != nil {
		e.t.Fatalf("GET %s: %v", path, err)
	}
	defer func() { _ = resp.Body.Close() }()
	payload, err := io.ReadAll(resp.Body)
	if err != nil {
		e.t.Fatalf("read body: %v", err)
	}
	return resp, payload
}

// lfWantETag 断言 ETag 是响应体的 sha256，并返回它。
// 校验值必须与写出的字节严格对应，否则客户端会拿旧体当新体。
func lfWantETag(t *testing.T, resp *http.Response, payload []byte) string {
	t.Helper()
	sum := sha256.Sum256(payload)
	want := `"` + hex.EncodeToString(sum[:]) + `"`
	if got := resp.Header.Get("ETag"); got != want {
		t.Fatalf("ETag = %q, want %q (body=%q)", got, want, payload)
	}
	return want
}

// lfWantNotModified 断言 304 的全部可见形状。
func lfWantNotModified(t *testing.T, resp *http.Response, payload []byte, etag string) {
	t.Helper()
	if resp.StatusCode != http.StatusNotModified {
		t.Fatalf("status = %d, want 304; body=%q", resp.StatusCode, payload)
	}
	if len(payload) != 0 {
		t.Fatalf("304 不得带体：%q", payload)
	}
	if got := resp.Header.Get("ETag"); got != etag {
		t.Fatalf("304 ETag = %q, want %q", got, etag)
	}
	for _, name := range []string{"Content-Type", "Content-Length"} {
		if got := resp.Header.Get(name); got != "" {
			t.Fatalf("304 不得声明 %s：%q", name, got)
		}
	}
}

// lfConditionalCase 是一种 If-None-Match 命中形状。
type lfConditionalCase struct {
	name  string
	lines []string
}

// lfConditionalCasesWithMismatch 追加一个不命中的形状。
func lfConditionalCasesWithMismatch(etag string) []lfConditionalCase {
	return append(lfConditionalCases(etag), lfConditionalCase{"mismatch", []string{`"other"`}})
}

// lfConditionalCases 是 If-None-Match 的命中形状。
func lfConditionalCases(etag string) []lfConditionalCase {
	return []lfConditionalCase{
		{"exact", []string{etag}},
		{"weak", []string{"W/" + etag}},
		{"comma list", []string{`"other", ` + etag}},
		{"weak in list", []string{`"other", W/` + etag}},
		{"star", []string{"*"}},
		{"repeated header lines", []string{`"other"`, etag}},
	}
}

// --- 账号列表 --------------------------------------------------------------

// 模拟前端每 10 秒轮询账号列表：校验值命中回 304，写后旧值失效。
func TestListFreshnessAccountsETag(t *testing.T) {
	e := newAuthEnv(t)
	schoolID := lfSeedSchool(t, e, "lf-a")
	ownerA := w2dSeedUser(t, e, "lf-owner-a@example.com", "owner-a-pass", coreauth.RoleUser)
	ownerB := w2dSeedUser(t, e, "lf-owner-b@example.com", "owner-b-pass", coreauth.RoleUser)
	a1 := lfSeedAccount(t, e, schoolID, ownerA.ID, "13800138101")
	a2 := lfSeedAccount(t, e, schoolID, ownerA.ID, "13800138102")
	b1 := lfSeedAccount(t, e, schoolID, ownerB.ID, "13800138103")

	session := e.w2dLoginAs("lf-owner-a@example.com", "owner-a-pass")
	resp, payload := lfGet(e, "/api/accounts", nil)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("GET /api/accounts = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	etagA := lfWantETag(t, resp, payload)
	var page lfAccountPage
	if err := json.Unmarshal(payload, &page); err != nil {
		t.Fatalf("decode accounts page: %v; body=%q", err, payload)
	}
	got := make(map[string]bool, len(page.Items))
	for _, item := range page.Items {
		got[item.ID] = true
		if item.OwnerUserID != ownerA.ID {
			t.Fatalf("user scope leaked owner %s (want %s)", item.OwnerUserID, ownerA.ID)
		}
	}
	if !got[a1] || !got[a2] || got[b1] {
		t.Fatalf("user scope = %v, want only own accounts %s/%s", got, a1, a2)
	}

	for _, tc := range lfConditionalCasesWithMismatch(etagA) {
		t.Run(tc.name, func(t *testing.T) {
			resp, payload := lfGet(e, "/api/accounts", tc.lines)
			if tc.name == "mismatch" {
				if resp.StatusCode != http.StatusOK {
					t.Fatalf("mismatch status = %d, want 200; body=%q", resp.StatusCode, payload)
				}
				if got := lfWantETag(t, resp, payload); got != etagA {
					t.Fatalf("mismatch ETag = %q, want %q", got, etagA)
				}
				return
			}
			lfWantNotModified(t, resp, payload, etagA)
		})
	}

	// 不同筛选与不同游标是不同表示，校验值必须跟着变。
	resp, payload = lfGet(e, "/api/accounts?q=no-such-remark", nil)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("filtered status = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	if got := lfWantETag(t, resp, payload); got == etagA {
		t.Fatal("筛选改变了响应体，ETag 不得相同")
	}

	resp, payload = lfGet(e, "/api/accounts?limit=1", nil)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("first page status = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	firstPageETag := lfWantETag(t, resp, payload)
	var firstPage lfAccountPage
	if err := json.Unmarshal(payload, &firstPage); err != nil {
		t.Fatalf("decode first page: %v; body=%q", err, payload)
	}
	if firstPage.NextCursor == "" {
		t.Fatal("limit=1 的两行账号必须给出 next_cursor")
	}
	resp, payload = lfGet(e, "/api/accounts?limit=1&cursor="+url.QueryEscape(firstPage.NextCursor), nil)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("second page status = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	if got := lfWantETag(t, resp, payload); got == firstPageETag {
		t.Fatal("游标翻页是不同表示，ETag 不得相同")
	}

	// 写操作改表示：旧 ETag 必须回 200 新体，而不是 304。
	// 一人一号：属主已有两行 live 种子，先软删再以同一属主身份建新行，
	// 写操作仍然改变该属主作用域的可见面。
	if _, err := e.db.Exec(e.ctx,
		"UPDATE accounts SET deleted_at = now() WHERE id = ANY($1::text[]::uuid[])",
		[]string{a1, a2}); err != nil {
		t.Fatalf("hide seeded accounts: %v", err)
	}
	created, createdBody := e.do(http.MethodPost, "/api/accounts", map[string]any{
		"school_id": schoolID,
		"phone":     "13800138123",
		"password":  "not-a-real-password",
	}, map[string]string{"X-CSRF-Token": session.CSRFToken})
	if created.StatusCode != http.StatusCreated {
		t.Fatalf("create account status = %d, want 201; body=%q", created.StatusCode, createdBody)
	}
	resp, payload = lfGet(e, "/api/accounts", []string{etagA})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("after write status = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	if got := lfWantETag(t, resp, payload); got == etagA {
		t.Fatal("写操作后旧 ETag 不得继续命中")
	}

	// 会话切换后旧 ETag 不能跨作用域复用：B 只看自己的行。
	e.w2dLoginAs("lf-owner-b@example.com", "owner-b-pass")
	resp, payload = lfGet(e, "/api/accounts", []string{etagA})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("other owner status = %d, want 200 (scope before 304); body=%q", resp.StatusCode, payload)
	}
	etagB := lfWantETag(t, resp, payload)
	if etagB == etagA {
		t.Fatal("不同作用域的响应体不同，ETag 不得相同")
	}
	var otherPage lfAccountPage
	if err := json.Unmarshal(payload, &otherPage); err != nil {
		t.Fatalf("decode other page: %v; body=%q", err, payload)
	}
	for _, item := range otherPage.Items {
		if item.ID == a1 || item.ID == a2 {
			t.Fatalf("cross-scope 304/200 leaked account %s", item.ID)
		}
		if item.OwnerUserID != ownerB.ID {
			t.Fatalf("user scope leaked owner %s (want %s)", item.OwnerUserID, ownerB.ID)
		}
	}
	resp, payload = lfGet(e, "/api/accounts", []string{etagB})
	lfWantNotModified(t, resp, payload, etagB)
}

// --- 用户列表 --------------------------------------------------------------

// 模拟 master 打开用户管理页：条件请求只省响应体，鉴权仍先判。
func TestListFreshnessUsersETag(t *testing.T) {
	e := newAuthEnv(t)
	master := w2dSeedUser(t, e, "lf-master@example.com", "master-pass", coreauth.RoleMaster)
	admin := w2dSeedUser(t, e, "lf-admin@example.com", "admin-pass", coreauth.RoleAdmin)
	userOne := w2dSeedUser(t, e, "lf-user-one@example.com", "user-one-pass", coreauth.RoleUser)
	userTwo := w2dSeedUser(t, e, "lf-user-two@example.com", "user-two-pass", coreauth.RoleUser)

	session := e.w2dLoginAs("lf-master@example.com", "master-pass")
	resp, payload := lfGet(e, "/api/users", nil)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("GET /api/users = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	etag := lfWantETag(t, resp, payload)
	var page lfUserPage
	if err := json.Unmarshal(payload, &page); err != nil {
		t.Fatalf("decode users page: %v; body=%q", err, payload)
	}
	got := make(map[string]bool, len(page.Items))
	for _, item := range page.Items {
		got[item.ID] = true
	}
	for _, id := range []string{master.ID, admin.ID, userOne.ID, userTwo.ID} {
		if !got[id] {
			t.Fatalf("master list missing user %s", id)
		}
	}

	for _, tc := range lfConditionalCasesWithMismatch(etag) {
		t.Run(tc.name, func(t *testing.T) {
			resp, payload := lfGet(e, "/api/users", tc.lines)
			if tc.name == "mismatch" {
				if resp.StatusCode != http.StatusOK {
					t.Fatalf("mismatch status = %d, want 200; body=%q", resp.StatusCode, payload)
				}
				if got := lfWantETag(t, resp, payload); got != etag {
					t.Fatalf("mismatch ETag = %q, want %q", got, etag)
				}
				return
			}
			lfWantNotModified(t, resp, payload, etag)
		})
	}

	resp, payload = lfGet(e, "/api/users?role=user", nil)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("filtered status = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	if got := lfWantETag(t, resp, payload); got == etag {
		t.Fatal("筛选改变了响应体，ETag 不得相同")
	}

	resp, payload = lfGet(e, "/api/users?limit=2", nil)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("first page status = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	firstPageETag := lfWantETag(t, resp, payload)
	var firstPage lfUserPage
	if err := json.Unmarshal(payload, &firstPage); err != nil {
		t.Fatalf("decode first page: %v; body=%q", err, payload)
	}
	if firstPage.NextCursor == "" {
		t.Fatal("limit=2 的四行用户必须给出 next_cursor")
	}
	resp, payload = lfGet(e, "/api/users?limit=2&cursor="+url.QueryEscape(firstPage.NextCursor), nil)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("second page status = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	if got := lfWantETag(t, resp, payload); got == firstPageETag {
		t.Fatal("游标翻页是不同表示，ETag 不得相同")
	}

	created, createdBody := e.do(http.MethodPost, "/api/users", map[string]any{
		"email":            "lf-created@example.com",
		"password":         "not-a-real-password",
		"role":             "user",
		"current_password": "master-pass",
	}, map[string]string{"X-CSRF-Token": session.CSRFToken})
	if created.StatusCode != http.StatusCreated {
		t.Fatalf("create user status = %d, want 201; body=%q", created.StatusCode, createdBody)
	}
	resp, payload = lfGet(e, "/api/users", []string{etag})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("after write status = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	if got := lfWantETag(t, resp, payload); got == etag {
		t.Fatal("写操作后旧 ETag 不得继续命中")
	}
	var afterWrite lfUserPage
	if err := json.Unmarshal(payload, &afterWrite); err != nil {
		t.Fatalf("decode after write: %v; body=%q", err, payload)
	}
	found := false
	for _, item := range afterWrite.Items {
		if item.Email == "lf-created@example.com" {
			found = true
		}
	}
	if !found {
		t.Fatal("写后响应体必须包含新用户")
	}

	// 鉴权先于 304：admin 拿着 master 的 ETag 也只能得到 403。
	e.w2dLoginAs("lf-admin@example.com", "admin-pass")
	resp, payload = lfGet(e, "/api/users", []string{etag})
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("admin status = %d, want 403 (auth before 304); body=%q", resp.StatusCode, payload)
	}
	if code, _ := errorCode(t, payload); code != "forbidden" {
		t.Fatalf("admin code = %q, want forbidden", code)
	}
	if got := resp.Header.Get("ETag"); got != "" {
		t.Fatalf("403 不得带 ETag：%q", got)
	}
}
