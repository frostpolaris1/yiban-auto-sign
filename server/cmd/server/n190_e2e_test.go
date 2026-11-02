package main

// 账号备注名与归属可读字段的 e2e。
//
// # 功能
// 钉住五条用户可见的性质：
//   1. 建号与编辑都接受备注名：trim、≤50 字、空串表示清空。
//   2. 搜索同时命中备注名、归属邮箱（含可见掩码）与手机号。
//   3. 只改备注名不把被拒账号送回待审；改凭据仍重提。
//   4. 响应只给归属邮箱掩码，明文邮箱不出服务端。
//   5. 超长备注名返回 400，库内值不变。
//
// # 归属
// cmd/server 可同时引用 adapters 与 entry；本文件用真实 PostgreSQL。
//
// # 口径
// 缺 DSN 时由 w2cNewEnv 打可见跳过横幅，跳过不是绿。
// 每个用例独占一个 schema。手机号与邮箱只取登记形状。

import (
	"net/http"
	"net/url"
	"strings"
	"testing"

	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

// n190Account 是本文件断言用的响应字段。
type n190Account struct {
	ID               string `json:"id"`
	Name             string `json:"name"`
	OwnerEmailMasked string `json:"owner_email_masked"`
	PhoneMasked      string `json:"phone_masked"`
	Status           string `json:"status"`
	RejectReason     string `json:"reject_reason"`
}

type n190Page struct {
	Items []n190Account `json:"items"`
}

// n190List 以指定会话列账号，返回整页与原始响应体。
func n190List(t *testing.T, e *w2cEnv, query string, issued *coreauth.Issued) (n190Page, string) {
	t.Helper()
	rec := e.do(http.MethodGet, "/api/accounts?q="+url.QueryEscape(query), nil, issued)
	w2cWantStatus(t, rec, http.StatusOK)
	var page n190Page
	e.decode(rec, &page)
	return page, rec.Body.String()
}

// n190IDs 把一页结果收成 id 集合，断言只看命中面。
func n190IDs(page n190Page) map[string]bool {
	out := make(map[string]bool, len(page.Items))
	for _, item := range page.Items {
		out[item.ID] = true
	}
	return out
}

// 建号可带备注名；编辑可改名与清空；响应带归属邮箱掩码。
func TestN190CreateAndUpdateName(t *testing.T) {
	e := w2cNewEnv(t)
	owner := e.user("n190.alpha@example.com", coreauth.RoleUser)
	school := e.school("n190-create")
	issued := e.login(owner)

	rec := e.do(http.MethodPost, "/api/accounts", map[string]any{
		"school_id": school.ID,
		"phone":     "13800000040",
		"password":  "secret",
		"name":      "  宿舍甲  ",
	}, issued)
	w2cWantStatus(t, rec, http.StatusCreated)
	var created n190Account
	e.decode(rec, &created)
	if created.Name != "宿舍甲" {
		t.Fatalf("created name = %q, want the trimmed value", created.Name)
	}
	if created.OwnerEmailMasked != "n***@example.com" {
		t.Fatalf("owner_email_masked = %q, want n***@example.com", created.OwnerEmailMasked)
	}
	if raw := "n190.alpha@example.com"; strings.Contains(rec.Body.String(), raw) {
		t.Fatalf("create response leaked the raw owner email")
	}

	got := n190Patch(t, e, created.ID, map[string]any{"name": "  宿舍乙  "}, issued)
	if got.Name != "宿舍乙" {
		t.Fatalf("name = %q, want the trimmed value", got.Name)
	}
	if got.Status != string(coreaccount.StatusPending) {
		t.Fatalf("status after a name edit = %q, want pending", got.Status)
	}
	detail := e.scalarString(
		"SELECT detail FROM audit_logs WHERE action = 'account.update' AND target_id = $1", created.ID)
	if detail != `{"field":"name"}` {
		t.Fatalf("account.update detail = %s, want name", detail)
	}

	cleared := n190Patch(t, e, created.ID, map[string]any{"name": ""}, issued)
	if cleared.Name != "" {
		t.Fatalf("name after clearing = %q, want empty", cleared.Name)
	}
	if stored := e.scalarString("SELECT name FROM accounts WHERE id = $1::uuid", created.ID); stored != "" {
		t.Fatalf("stored name after clearing = %q, want empty", stored)
	}
}

// 超长备注名在入口被拒：库内值不变，错误码是 validation。
func TestN190NameLengthLimit(t *testing.T) {
	e := w2cNewEnv(t)
	owner := e.user("n190.length@example.com", coreauth.RoleUser)
	school := e.school("n190-length")
	issued := e.login(owner)

	tooLong := strings.Repeat("测", 51)
	rec := e.do(http.MethodPost, "/api/accounts", map[string]any{
		"school_id": school.ID,
		"phone":     "13800000040",
		"password":  "secret",
		"name":      tooLong,
	}, issued)
	w2cWantStatus(t, rec, http.StatusBadRequest)
	if code := e.errorCode(rec); code != "validation_failed" {
		t.Fatalf("create code = %q, want validation_failed", code)
	}

	id := e.account(school.ID, owner.ID, "13800000041")
	rec = e.do(http.MethodPatch, "/api/accounts/"+id, map[string]any{"name": tooLong}, issued)
	w2cWantStatus(t, rec, http.StatusBadRequest)
	if code := e.errorCode(rec); code != "validation_failed" {
		t.Fatalf("patch code = %q, want validation_failed", code)
	}
	if stored := e.scalarString("SELECT name FROM accounts WHERE id = $1::uuid", id); stored != "" {
		t.Fatalf("stored name = %q, want the old empty value", stored)
	}
}

// 只改备注名不重提被拒账号；改凭据仍回 pending。
func TestN190NameOnlyEditKeepsRejected(t *testing.T) {
	e := w2cNewEnv(t)
	owner := e.user("n190.rejected@example.com", coreauth.RoleUser)
	admin := e.user("n190.rejected-admin@example.com", coreauth.RoleAdmin)
	school := e.school("n190-rejected")
	e.bind(admin.ID, school.ID)
	id := e.account(school.ID, owner.ID, "13800000040")
	aogSetAccount(t, e, id, string(coreaccount.StatusRejected), "资料不全", admin.ID)

	got := n190Patch(t, e, id, map[string]any{"name": "待补照片"}, e.login(owner))
	if got.Status != string(coreaccount.StatusRejected) {
		t.Fatalf("status after a name-only edit = %q, want rejected", got.Status)
	}
	if got.RejectReason != "资料不全" {
		t.Fatalf("reject_reason after a name-only edit = %q, want the original reason", got.RejectReason)
	}
	if n := aogCount(t, e, "account.resubmit", id); n != 0 {
		t.Fatalf("name-only edit wrote %d resubmit rows; want 0", n)
	}

	// 同值手机号也不是凭据编辑：与备注名一起提交仍不重提。
	if got := n190Patch(t, e, id, map[string]any{
		"name":  "待补照片",
		"phone": "13800000040",
	}, e.login(owner)); got.Status != string(coreaccount.StatusRejected) {
		t.Fatalf("name + unchanged phone = %q, want rejected", got.Status)
	}

	// 改凭据仍是唯一的重提路径。
	if got := n190Patch(t, e, id, map[string]any{"password": "fixed-secret"}, e.login(owner)); got.Status != string(coreaccount.StatusPending) {
		t.Fatalf("credential edit = %q, want pending", got.Status)
	}
	if n := aogCount(t, e, "account.resubmit", id); n != 1 {
		t.Fatalf("credential edit wrote %d resubmit rows; want 1", n)
	}
}

// 搜索同时命中备注名、归属邮箱（含可见掩码）与手机号；明文邮箱不出现在任何响应。
func TestN190SearchNameEmailAndPhone(t *testing.T) {
	e := w2cNewEnv(t)
	ownerA := e.user("alpha.owner@example.com", coreauth.RoleUser)
	ownerA2 := e.user("a.other@example.com", coreauth.RoleUser)
	ownerB := e.user("beta.owner@example.com", coreauth.RoleUser)
	school := e.school("n190-search")
	master := e.user("n190.master@example.com", coreauth.RoleMaster)

	east := e.account(school.ID, ownerA.ID, "13800000040")
	west := e.account(school.ID, ownerA2.ID, "13900000041")
	teach := e.account(school.ID, ownerB.ID, "13500000042")
	for id, name := range map[string]string{
		east:  "东区宿舍 East Dorm",
		west:  "西区宿舍",
		teach: "教学楼下 100%栋",
	} {
		if _, err := e.db.Exec(e.ctx, "UPDATE accounts SET name = $2 WHERE id = $1::uuid", id, name); err != nil {
			t.Fatalf("seed name for %s: %v", id, err)
		}
	}

	issued := e.login(master)

	// 备注名：中文子串与大小写不敏感。
	if page, _ := n190List(t, e, "宿舍", issued); len(page.Items) != 2 ||
		!n190IDs(page)[east] || !n190IDs(page)[west] {
		t.Fatalf("q=宿舍 ids = %v, want the two dorm accounts", n190IDs(page))
	}
	if page, _ := n190List(t, e, "east", issued); !n190IDs(page)[east] || len(page.Items) != 1 {
		t.Fatalf("q=east ids = %v, want only the east dorm", n190IDs(page))
	}
	// % 是字面子串，不是 LIKE 通配符。
	if page, _ := n190List(t, e, "100%", issued); !n190IDs(page)[teach] || len(page.Items) != 1 {
		t.Fatalf("q=100%% ids = %v, want only the percent-named row", n190IDs(page))
	}

	// 归属邮箱：原始子串。
	if page, _ := n190List(t, e, "beta.owner", issued); !n190IDs(page)[teach] || len(page.Items) != 1 {
		t.Fatalf("q=beta.owner ids = %v, want only the beta owner's row", n190IDs(page))
	}
	// 可见掩码：a***@example.com 命中两个同掩码的属主，不命中 b***。
	if page, _ := n190List(t, e, "a***@example.com", issued); !n190IDs(page)[east] ||
		!n190IDs(page)[west] || n190IDs(page)[teach] || len(page.Items) != 2 {
		t.Fatalf("masked email ids = %v, want the two a*** rows", n190IDs(page))
	}

	// 手机号：前缀、后缀、全号仍按原口径。
	if page, _ := n190List(t, e, "138", issued); !n190IDs(page)[east] {
		t.Fatalf("q=138 ids = %v, want the 138 account", n190IDs(page))
	}
	if page, _ := n190List(t, e, "0041", issued); !n190IDs(page)[west] {
		t.Fatalf("q=0041 ids = %v, want the suffix match", n190IDs(page))
	}
	if page, _ := n190List(t, e, "13500000042", issued); !n190IDs(page)[teach] || len(page.Items) != 1 {
		t.Fatalf("q=13500000042 ids = %v, want the exact account", n190IDs(page))
	}

	// 明文邮箱不进任何响应体。
	for _, body := range e.bodies {
		for _, raw := range []string{"alpha.owner@example.com", "a.other@example.com", "beta.owner@example.com"} {
			if strings.Contains(body, raw) {
				t.Fatalf("response body leaked the raw owner email %q", raw)
			}
		}
	}
}

// 列表与详情都带掩码归属邮箱：同一条账号在两种读法下同值。
func TestN190OwnerEmailMaskedOnRead(t *testing.T) {
	e := w2cNewEnv(t)
	owner := e.user("n190.read@example.com", coreauth.RoleUser)
	school := e.school("n190-read")
	id := e.account(school.ID, owner.ID, "13800000040")
	issued := e.login(owner)

	page, body := n190List(t, e, "", issued)
	if len(page.Items) != 1 || page.Items[0].OwnerEmailMasked != "n***@example.com" {
		t.Fatalf("list owner_email_masked = %+v, want n***@example.com", page.Items)
	}
	if strings.Contains(body, "n190.read@example.com") {
		t.Fatalf("list body leaked the raw owner email")
	}

	rec := e.do(http.MethodGet, "/api/accounts/"+id, nil, issued)
	w2cWantStatus(t, rec, http.StatusOK)
	var got n190Account
	e.decode(rec, &got)
	if got.OwnerEmailMasked != "n***@example.com" {
		t.Fatalf("detail owner_email_masked = %q", got.OwnerEmailMasked)
	}
	if strings.Contains(rec.Body.String(), "n190.read@example.com") {
		t.Fatalf("detail body leaked the raw owner email")
	}
}

// n190Patch 发一次 PATCH 并解出账号。
func n190Patch(t *testing.T, e *w2cEnv, id string, body map[string]any, issued *coreauth.Issued) n190Account {
	t.Helper()
	rec := e.do(http.MethodPatch, "/api/accounts/"+id, body, issued)
	w2cWantStatus(t, rec, http.StatusOK)
	var got n190Account
	e.decode(rec, &got)
	return got
}
