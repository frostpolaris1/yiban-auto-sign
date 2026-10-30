package main

// G1U e2e：P5 图片剩余项。
//
// # 功能
// 用真实 PostgreSQL 与临时 blob 目录钉住三件事：
// ①admin 读绑定学校账号的照片；②按账号列照片；③学校默认图读写。
// 本文件**不**测图片预处理（P5-B 已覆盖）。
//
// # 归属
// cmd/server 的 e2e。复用 w2cEnv 的 schema 隔离与 HTTP 客户端。
//
// # 口径
// 模拟的真实用户动作：管理员打开账号详情看照片、设置学校默认图。
// 审计只断言行存在、detail 键与值、无 PII；不断言链内序。
// 合成值：手机号取 tests/synthetic-fixtures.json 登记项。

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"
	"time"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

// g1uPhoto 是图片页的单行形状（与 Photo schema 对齐）。
type g1uPhoto struct {
	ID        string    `json:"id"`
	Sha256    string    `json:"sha256"`
	Mime      string    `json:"mime"`
	Width     int       `json:"width"`
	Height    int       `json:"height"`
	Bytes     int64     `json:"bytes"`
	CreatedAt time.Time `json:"created_at"`
}

type g1uPhotoPage struct {
	Items      []g1uPhoto `json:"items"`
	NextCursor string     `json:"next_cursor"`
}

// g1uDefaultPhoto 是学校默认图响应形状。
type g1uDefaultPhoto struct {
	PhotoID *string `json:"photo_id"`
	ETag    string  `json:"etag"`
}

// g1uFixture 是三个用例共用的角色与账号布局。
//
// 院校 A 绑定 adminA；院校 B 绑定 adminB。
// owner1 在 A 有账号；owner2 在 A 与 B 都有账号；owner3 没有账号。
type g1uFixture struct {
	e *w2cEnv

	schoolA, schoolB string
	acctA1, acctA2   string
	acctB2           string

	owner1 *coreauth.Issued
	owner2 *coreauth.Issued
	owner3 *coreauth.Issued
	adminA *coreauth.Issued
	adminB *coreauth.Issued
	master *coreauth.Issued
}

func g1uSetup(t *testing.T) *g1uFixture {
	t.Helper()
	e := w2cNewEnv(t)
	schoolA := e.school("g1u-a")
	schoolB := e.school("g1u-b")

	owner1 := e.user("g1u-owner1@example.com", coreauth.RoleUser)
	owner2 := e.user("g1u-owner2@example.com", coreauth.RoleUser)
	owner3 := e.user("g1u-owner3@example.com", coreauth.RoleUser)
	adminAUser := e.user("g1u-admin-a@example.com", coreauth.RoleAdmin)
	adminBUser := e.user("g1u-admin-b@example.com", coreauth.RoleAdmin)
	masterUser := e.user("g1u-master@example.com", coreauth.RoleMaster)
	e.bind(adminAUser.ID, schoolA.ID)
	e.bind(adminBUser.ID, schoolB.ID)

	return &g1uFixture{
		e:       e,
		schoolA: schoolA.ID,
		schoolB: schoolB.ID,
		acctA1:  e.account(schoolA.ID, owner1.ID, "13800020001"),
		acctA2:  e.account(schoolA.ID, owner2.ID, "13800020002"),
		acctB2:  e.account(schoolB.ID, owner2.ID, "13800020003"),
		owner1:  e.login(owner1),
		owner2:  e.login(owner2),
		owner3:  e.login(owner3),
		adminA:  e.login(adminAUser),
		adminB:  e.login(adminBUser),
		master:  e.login(masterUser),
	}
}

// g1uUpload 上传一张图并返回传输形状。
func g1uUpload(t *testing.T, e *w2cEnv, issued *coreauth.Issued, w, h int) g1uPhoto {
	t.Helper()
	rec := e.uploadPhoto(issued, pngPhoto(t, w, h))
	if rec.Code != http.StatusCreated {
		t.Fatalf("upload status = %d: %s", rec.Code, rec.Body.String())
	}
	var p g1uPhoto
	if err := json.Unmarshal(rec.Body.Bytes(), &p); err != nil {
		t.Fatalf("decode photo: %v; body=%s", err, rec.Body.String())
	}
	return p
}

// g1uPage 发一次 GET 并解析图片页。
func g1uPage(t *testing.T, e *w2cEnv, path string, issued *coreauth.Issued) (g1uPhotoPage, string) {
	t.Helper()
	rec := e.do("GET", path, nil, issued)
	if rec.Code != http.StatusOK {
		t.Fatalf("GET %s status = %d: %s", path, rec.Code, rec.Body.String())
	}
	var page g1uPhotoPage
	if err := json.Unmarshal(rec.Body.Bytes(), &page); err != nil {
		t.Fatalf("decode page: %v; body=%s", err, rec.Body.String())
	}
	return page, rec.Body.String()
}

// g1uPutDefault 发一次默认图写入并解析响应。
func g1uPutDefault(t *testing.T, e *w2cEnv, schoolID string, issued *coreauth.Issued, body any) (int, string) {
	t.Helper()
	rec := e.do("PUT", "/api/schools/"+schoolID+"/default-photo", body, issued)
	return rec.Code, rec.Body.String()
}

// 用例一：admin 读绑定学校账号的照片；跨校、他人与不存在统一 404。
func TestG1UAdminReadsBoundSchoolPhoto(t *testing.T) {
	f := g1uSetup(t)
	e := f.e
	photo := g1uUpload(t, e, f.owner1, 12, 8)
	path := "/api/me/photos/" + photo.ID

	// 绑定 A 的 admin 读 A 校账号属主的照片：200，字节与 ETag 完整。
	rec := e.do("GET", path, nil, f.adminA)
	if rec.Code != http.StatusOK {
		t.Fatalf("admin 本校读取 status = %d: %s", rec.Code, rec.Body.String())
	}
	if got := rec.Header().Get("Content-Type"); got != "image/png" {
		t.Fatalf("content-type = %q", got)
	}
	if got := rec.Header().Get("ETag"); got != `"`+photo.Sha256+`"` {
		t.Fatalf("etag = %q, want %q", got, photo.Sha256)
	}
	if rec.Body.Len() == 0 {
		t.Fatal("admin 读到空字节")
	}

	// 跨校 admin：404。
	if rec := e.do("GET", path, nil, f.adminB); rec.Code != http.StatusNotFound {
		t.Fatalf("跨校 admin status = %d: %s", rec.Code, rec.Body.String())
	}
	// 非属主 user：404。
	if rec := e.do("GET", path, nil, f.owner2); rec.Code != http.StatusNotFound {
		t.Fatalf("他人 user status = %d", rec.Code)
	}
	// master 没有跨属主读取面：属主未命中即 404。
	if rec := e.do("GET", path, nil, f.master); rec.Code != http.StatusNotFound {
		t.Fatalf("master status = %d", rec.Code)
	}
	// 不存在的照片：404。
	missing := "01900000-0000-7000-8000-0000000000ff"
	if rec := e.do("GET", "/api/me/photos/"+missing, nil, f.adminA); rec.Code != http.StatusNotFound {
		t.Fatalf("不存在照片 status = %d", rec.Code)
	}
	// 属主自己：200（既有行为不变）。
	if rec := e.do("GET", path, nil, f.owner1); rec.Code != http.StatusOK {
		t.Fatalf("属主读取 status = %d", rec.Code)
	}

	// 账号软删后 admin 失去读取面：A 校不再有该属主的账号。
	if rec := e.do("DELETE", "/api/accounts/"+f.acctA1, nil, f.owner1); rec.Code != http.StatusNoContent {
		t.Fatalf("删号 status = %d: %s", rec.Code, rec.Body.String())
	}
	if rec := e.do("GET", path, nil, f.adminA); rec.Code != http.StatusNotFound {
		t.Fatalf("删号后 admin status = %d", rec.Code)
	}
}

// 用例二：按账号列照片。三种角色 × 越权组合；分页形状与 /api/me/photos 一致。
func TestG1UAccountPhotoList(t *testing.T) {
	f := g1uSetup(t)
	e := f.e
	p1 := g1uUpload(t, e, f.owner1, 12, 8)
	p2 := g1uUpload(t, e, f.owner1, 8, 12)
	g1uUpload(t, e, f.owner2, 10, 10)

	// owner1 读自己账号：200，两行，与 /api/me/photos 字节相等。
	page, body := g1uPage(t, e, "/api/accounts/"+f.acctA1+"/photos", f.owner1)
	if len(page.Items) != 2 {
		t.Fatalf("items = %d, want 2", len(page.Items))
	}
	seen := map[string]bool{}
	for _, item := range page.Items {
		seen[item.ID] = true
	}
	if !seen[p1.ID] || !seen[p2.ID] {
		t.Fatalf("items = %+v, want %s and %s", page.Items, p1.ID, p2.ID)
	}
	_, meBody := g1uPage(t, e, "/api/me/photos", f.owner1)
	if body != meBody {
		t.Fatalf("账号图片页与 /api/me/photos 形状不一致：\naccount=%s\nme=%s", body, meBody)
	}

	// admin 限绑定学校：本校账号 200。
	g1uPage(t, e, "/api/accounts/"+f.acctA1+"/photos", f.adminA)
	for _, tc := range []struct {
		name   string
		acct   string
		issued *coreauth.Issued
		want   int
	}{
		{"跨校 admin", f.acctB2, f.adminA, http.StatusNotFound},
		{"他人 user", f.acctB2, f.owner1, http.StatusNotFound},
		{"同校他人账号 user", f.acctA2, f.owner1, http.StatusNotFound},
		{"master", f.acctA1, f.master, http.StatusForbidden},
		{"不存在账号 admin", "01900000-0000-7000-8000-0000000000fe", f.adminA, http.StatusNotFound},
	} {
		rec := e.do("GET", "/api/accounts/"+tc.acct+"/photos", nil, tc.issued)
		if rec.Code != tc.want {
			t.Fatalf("%s status = %d, want %d: %s", tc.name, rec.Code, tc.want, rec.Body.String())
		}
	}

	// 同校他人账号由绑定校 admin 读取：200。
	if rec := e.do("GET", "/api/accounts/"+f.acctA2+"/photos", nil, f.adminA); rec.Code != http.StatusOK {
		t.Fatalf("admin 读同校账号 status = %d", rec.Code)
	}
	// 分页：limit=1 给游标，第二页接着走；limit 越界 400。
	first := e.do("GET", "/api/accounts/"+f.acctA1+"/photos?limit=1", nil, f.owner1)
	if first.Code != http.StatusOK {
		t.Fatalf("limit=1 status = %d", first.Code)
	}
	var page1 g1uPhotoPage
	if err := json.Unmarshal(first.Body.Bytes(), &page1); err != nil {
		t.Fatalf("decode page1: %v", err)
	}
	if len(page1.Items) != 1 || page1.NextCursor == "" {
		t.Fatalf("page1 = %+v, want 1 item and a cursor", page1.Items)
	}
	second := e.do("GET", "/api/accounts/"+f.acctA1+"/photos?limit=1&cursor="+page1.NextCursor, nil, f.owner1)
	if second.Code != http.StatusOK {
		t.Fatalf("second page status = %d: %s", second.Code, second.Body.String())
	}
	var page2 g1uPhotoPage
	if err := json.Unmarshal(second.Body.Bytes(), &page2); err != nil {
		t.Fatalf("decode page2: %v", err)
	}
	if len(page2.Items) != 1 || page2.Items[0].ID == page1.Items[0].ID {
		t.Fatalf("page2 = %+v", page2.Items)
	}
	if rec := e.do("GET", "/api/accounts/"+f.acctA1+"/photos?limit=201", nil, f.owner1); rec.Code != http.StatusBadRequest {
		t.Fatalf("limit=201 status = %d", rec.Code)
	}
}

// 用例三：学校默认图读写、可见面、幂等、审计。
func TestG1USchoolDefaultPhoto(t *testing.T) {
	f := g1uSetup(t)
	e := f.e
	p1 := g1uUpload(t, e, f.owner1, 12, 8)
	p2 := g1uUpload(t, e, f.owner2, 8, 12) // owner2 在 A 有账号：adminA 可见
	p3 := g1uUpload(t, e, f.owner3, 6, 6)  // owner3 无账号：adminA 不可见

	// 初始未设置：200 且 photo_id 键存在取值 null。
	rec := e.do("GET", "/api/schools/"+f.schoolA+"/default-photo", nil, f.adminA)
	if rec.Code != http.StatusOK {
		t.Fatalf("初始 GET status = %d: %s", rec.Code, rec.Body.String())
	}
	if body := rec.Body.String(); !strings.Contains(body, `"photo_id":null`) {
		t.Fatalf("初始响应缺 null 键：%s", body)
	}
	var got g1uDefaultPhoto
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatalf("decode default: %v", err)
	}
	if got.PhotoID != nil || got.ETag != "" {
		t.Fatalf("初始值 = %+v", got)
	}

	// adminA 设 p1：200，回显一致。
	code, body := g1uPutDefault(t, e, f.schoolA, f.adminA, map[string]any{"photo_id": p1.ID})
	if code != http.StatusOK {
		t.Fatalf("PUT status = %d: %s", code, body)
	}
	if err := json.Unmarshal([]byte(body), &got); err != nil {
		t.Fatalf("decode PUT: %v", err)
	}
	if got.PhotoID == nil || *got.PhotoID != p1.ID || got.ETag != p1.Sha256 {
		t.Fatalf("PUT 回显 = %+v, want %s/%s", got, p1.ID, p1.Sha256)
	}

	// GET 一致：master、adminA、owner1（A 校账号属主）都读到 p1。
	for _, tc := range []struct {
		name   string
		issued *coreauth.Issued
	}{
		{"adminA", f.adminA},
		{"master", f.master},
		{"owner1", f.owner1},
	} {
		rec := e.do("GET", "/api/schools/"+f.schoolA+"/default-photo", nil, tc.issued)
		if rec.Code != http.StatusOK {
			t.Fatalf("%s GET status = %d: %s", tc.name, rec.Code, rec.Body.String())
		}
		var cur g1uDefaultPhoto
		if err := json.Unmarshal(rec.Body.Bytes(), &cur); err != nil {
			t.Fatalf("%s decode: %v", tc.name, err)
		}
		if cur.PhotoID == nil || *cur.PhotoID != p1.ID || cur.ETag != p1.Sha256 {
			t.Fatalf("%s GET = %+v", tc.name, cur)
		}
	}

	// 可见面：无该校账号的 user 404；跨校 admin 404；不存在学校 404。
	if rec := e.do("GET", "/api/schools/"+f.schoolA+"/default-photo", nil, f.owner3); rec.Code != http.StatusNotFound {
		t.Fatalf("无账号 user GET status = %d", rec.Code)
	}
	if rec := e.do("GET", "/api/schools/"+f.schoolA+"/default-photo", nil, f.adminB); rec.Code != http.StatusNotFound {
		t.Fatalf("跨校 admin GET status = %d", rec.Code)
	}
	if rec := e.do("GET", "/api/schools/01900000-0000-7000-8000-0000000000fd/default-photo", nil, f.master); rec.Code != http.StatusNotFound {
		t.Fatalf("不存在学校 GET status = %d", rec.Code)
	}

	// 幂等：重复 PUT 同值 200，GET 不变。
	if code, body := g1uPutDefault(t, e, f.schoolA, f.adminA, map[string]any{"photo_id": p1.ID}); code != http.StatusOK {
		t.Fatalf("重复 PUT status = %d: %s", code, body)
	}
	var again g1uDefaultPhoto
	rec = e.do("GET", "/api/schools/"+f.schoolA+"/default-photo", nil, f.adminA)
	if err := json.Unmarshal(rec.Body.Bytes(), &again); err != nil {
		t.Fatalf("decode after repeat: %v", err)
	}
	if again.PhotoID == nil || *again.PhotoID != p1.ID {
		t.Fatalf("重复 PUT 后 = %+v", again)
	}

	// adminA 设 p2（属主在 A 有账号）：200。
	if code, body := g1uPutDefault(t, e, f.schoolA, f.adminA, map[string]any{"photo_id": p2.ID}); code != http.StatusOK {
		t.Fatalf("可见他账号图 PUT status = %d: %s", code, body)
	}
	// adminA 设 p3（属主不在 A）：400。
	if code, body := g1uPutDefault(t, e, f.schoolA, f.adminA, map[string]any{"photo_id": p3.ID}); code != http.StatusBadRequest {
		t.Fatalf("不可见图 PUT status = %d: %s", code, body)
	}
	// 不存在的照片：400。
	if code, _ := g1uPutDefault(t, e, f.schoolA, f.adminA, map[string]any{"photo_id": "01900000-0000-7000-8000-0000000000fc"}); code != http.StatusBadRequest {
		t.Fatalf("不存在图 PUT status = %d", code)
	}
	// 形状非法：400。
	if code, _ := g1uPutDefault(t, e, f.schoolA, f.adminA, map[string]any{"photo_id": "not-a-uuid"}); code != http.StatusBadRequest {
		t.Fatalf("非法形状 PUT status = %d", code)
	}
	// master 写：403（契约把 PUT default-photo 收窄为 admin）。
	if code, body := g1uPutDefault(t, e, f.schoolA, f.master, map[string]any{"photo_id": p3.ID}); code != http.StatusForbidden {
		t.Fatalf("master PUT status = %d: %s", code, body)
	}
	// user 写：403。
	if code, _ := g1uPutDefault(t, e, f.schoolA, f.owner1, map[string]any{"photo_id": p1.ID}); code != http.StatusForbidden {
		t.Fatalf("user PUT status = %d", code)
	}
	// 跨校 admin 写：404。
	if code, _ := g1uPutDefault(t, e, f.schoolA, f.adminB, map[string]any{"photo_id": p1.ID}); code != http.StatusNotFound {
		t.Fatalf("跨校 admin PUT status = %d", code)
	}
	// master 写不存在学校：403（角色门先于院校判定）。
	if code, _ := g1uPutDefault(t, e, "01900000-0000-7000-8000-0000000000fc", f.master, map[string]any{"photo_id": p1.ID}); code != http.StatusForbidden {
		t.Fatalf("不存在学校 PUT status = %d", code)
	}
	// 匿名：401。
	if rec := e.do("GET", "/api/schools/"+f.schoolA+"/default-photo", nil, nil); rec.Code != http.StatusUnauthorized {
		t.Fatalf("匿名 GET status = %d", rec.Code)
	}
	if rec := e.do("PUT", "/api/schools/"+f.schoolA+"/default-photo", map[string]any{"photo_id": nil}, nil); rec.Code != http.StatusUnauthorized {
		t.Fatalf("匿名 PUT status = %d", rec.Code)
	}

	// 清除：PUT null → 200 null；GET null。
	if code, body := g1uPutDefault(t, e, f.schoolA, f.adminA, map[string]any{"photo_id": nil}); code != http.StatusOK {
		t.Fatalf("清除 PUT status = %d: %s", code, body)
	}
	rec = e.do("GET", "/api/schools/"+f.schoolA+"/default-photo", nil, f.adminA)
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatalf("decode cleared: %v", err)
	}
	if got.PhotoID != nil || got.ETag != "" {
		t.Fatalf("清除后 = %+v", got)
	}

	// 审计：最近一行是清除。detail 只有 photo_id 与 previous_photo_id，无 PII。
	// 成功写入 4 次（p1、重复 p1、p2、清除），每次一行。
	detail := e.scalarString(
		`SELECT detail FROM audit_logs WHERE action = $1 ORDER BY id DESC LIMIT 1`,
		"school.default_photo_set")
	var d map[string]any
	if err := json.Unmarshal([]byte(detail), &d); err != nil {
		t.Fatalf("解析 detail: %v", err)
	}
	if len(d) != 2 {
		t.Fatalf("detail 键 = %v, want 恰好 photo_id 与 previous_photo_id", d)
	}
	if d["photo_id"] != nil {
		t.Fatalf("清除后 detail.photo_id = %v, want null", d["photo_id"])
	}
	if d["previous_photo_id"] != p2.ID {
		t.Fatalf("清除后 detail.previous_photo_id = %v, want %s", d["previous_photo_id"], p2.ID)
	}
	for _, pii := range []string{"g1u-owner", "@example.com", "13800020001", "13800020002", "13800020003"} {
		if strings.Contains(detail, pii) {
			t.Fatalf("审计 detail 含 PII %q: %s", pii, detail)
		}
	}
	n := e.scalarInt(`SELECT count(*) FROM audit_logs WHERE action = 'school.default_photo_set'`)
	if n != 4 {
		t.Fatalf("审计行 = %d, want 4（每次成功写入一行）", n)
	}

	// 引用照片软删后 GET 按未设置回：不返回 API 已判定不存在的 id。
	if code, body := g1uPutDefault(t, e, f.schoolA, f.adminA, map[string]any{"photo_id": p1.ID}); code != http.StatusOK {
		t.Fatalf("重设 p1 status = %d: %s", code, body)
	}
	if rec := e.do("DELETE", "/api/me/photos/"+p1.ID, nil, f.owner1); rec.Code != http.StatusNoContent {
		t.Fatalf("删图 status = %d", rec.Code)
	}
	rec = e.do("GET", "/api/schools/"+f.schoolA+"/default-photo", nil, f.adminA)
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatalf("decode after photo delete: %v", err)
	}
	if got.PhotoID != nil || got.ETag != "" {
		t.Fatalf("引用照片软删后 = %+v, want null", got)
	}
	// 同字节重传恢复同一行：引用随之复活。
	back := g1uUpload(t, e, f.owner1, 12, 8)
	if back.ID != p1.ID {
		t.Fatalf("重传未恢复同一行: %s, want %s", back.ID, p1.ID)
	}
	rec = e.do("GET", "/api/schools/"+f.schoolA+"/default-photo", nil, f.adminA)
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatalf("decode after restore: %v", err)
	}
	if got.PhotoID == nil || *got.PhotoID != p1.ID {
		t.Fatalf("重传后 = %+v, want %s", got, p1.ID)
	}

	// 缺 photo_id 键：400（契约 required；显式 null 才是清除）。不写审计。
	before := e.scalarInt(`SELECT count(*) FROM audit_logs WHERE action = 'school.default_photo_set'`)
	if rec := e.do("PUT", "/api/schools/"+f.schoolA+"/default-photo", map[string]any{}, f.adminA); rec.Code != http.StatusBadRequest {
		t.Fatalf("缺键 PUT status = %d: %s", rec.Code, rec.Body.String())
	}
	if after := e.scalarInt(`SELECT count(*) FROM audit_logs WHERE action = 'school.default_photo_set'`); after != before {
		t.Fatalf("缺键 PUT 写了审计行：%d -> %d", before, after)
	}

	// 路径里的大写 UUID：写成功且审计 TargetID 规范化为小写。
	if code, body := g1uPutDefault(t, e, strings.ToUpper(f.schoolA), f.adminA, map[string]any{"photo_id": p1.ID}); code != http.StatusOK {
		t.Fatalf("大写校 id PUT status = %d: %s", code, body)
	}
	target := e.scalarString(`SELECT target_id FROM audit_logs WHERE action = 'school.default_photo_set' ORDER BY id DESC LIMIT 1`)
	if target != strings.ToLower(f.schoolA) {
		t.Fatalf("审计 target = %q, want 规范化小写 %q", target, strings.ToLower(f.schoolA))
	}
}

// 既有四端点回归：user 的列表、上传、读取、软删行为不变。
func TestG1UPhotoEndpointsUnchanged(t *testing.T) {
	f := g1uSetup(t)
	e := f.e
	photo := g1uUpload(t, e, f.owner1, 10, 10)

	page, _ := g1uPage(t, e, "/api/me/photos", f.owner1)
	if len(page.Items) != 1 || page.Items[0].ID != photo.ID {
		t.Fatalf("列表 = %+v", page.Items)
	}
	if rec := e.do("GET", "/api/me/photos/"+photo.ID, nil, f.owner1); rec.Code != http.StatusOK {
		t.Fatalf("属主读取 status = %d", rec.Code)
	}
	if rec := e.do("GET", "/api/me/photos/"+photo.ID, nil, f.owner2); rec.Code != http.StatusNotFound {
		t.Fatalf("他人读取 status = %d", rec.Code)
	}
	if rec := e.do("DELETE", "/api/me/photos/"+photo.ID, nil, f.owner1); rec.Code != http.StatusNoContent {
		t.Fatalf("删除 status = %d", rec.Code)
	}
	if rec := e.do("GET", "/api/me/photos/"+photo.ID, nil, f.owner1); rec.Code != http.StatusNotFound {
		t.Fatalf("删除后读取 status = %d", rec.Code)
	}
	// 未绑定任何学校的 admin：/me 列表按 owner 作用域回 200（空库）。
	// 读取他人照片仍 404：admin 的跨属主面需要绑定校。
	unbound := e.user("g1u-admin-unbound@example.com", coreauth.RoleAdmin)
	unboundIssued := e.login(unbound)
	if rec := e.do("GET", "/api/me/photos", nil, unboundIssued); rec.Code != http.StatusOK {
		t.Fatalf("未绑定 admin 列表 status = %d", rec.Code)
	}
	if page, _ := g1uPage(t, e, "/api/me/photos", unboundIssued); len(page.Items) != 0 {
		t.Fatalf("未绑定 admin 自有库 = %+v, want empty", page.Items)
	}
	if rec := e.do("GET", "/api/me/photos/"+photo.ID, nil, unboundIssued); rec.Code != http.StatusNotFound {
		t.Fatalf("未绑定 admin 读取 status = %d", rec.Code)
	}
}
