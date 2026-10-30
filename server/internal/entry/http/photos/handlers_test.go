package photos_test

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"mime/multipart"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appphotos "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/photos"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corephoto "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/photo"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	photoshttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/photos"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/router"
)

// ---- 测试替身 ----

type fakeService struct {
	page    appphotos.Page
	pageErr error

	uploadRec corephoto.Record
	uploadErr error
	lastRaw   []byte

	getRec  corephoto.Record
	getBody io.ReadCloser
	getErr  error
	// openCalls 计 Open 次数：304 命中路径不得打开 blob 流。
	openCalls int

	deleteErr error

	retentionDays int
	retentionErr  error

	// scopes 记录每个调用收到的作用域，钉住 /api/me/* 的归属口径。
	scopes []corescope.Scope

	setDefaultOut   appphotos.SchoolDefaultPhoto
	setDefaultErr   error
	setDefaultScope corescope.Scope
}

func (f *fakeService) Upload(_ context.Context, scope corescope.Scope, _ appaudit.Actor, raw []byte) (corephoto.Record, error) {
	f.scopes = append(f.scopes, scope)
	if scope.Kind() != corescope.KindUser {
		return corephoto.Record{}, errs.New(errs.CodeForbidden, "photo library requires an owner scope")
	}
	f.lastRaw = append([]byte(nil), raw...)
	return f.uploadRec, f.uploadErr
}

func (f *fakeService) List(_ context.Context, scope corescope.Scope, _ string, _ int) (appphotos.Page, error) {
	f.scopes = append(f.scopes, scope)
	return f.page, f.pageErr
}

func (f *fakeService) Get(context.Context, corescope.Scope, string) (corephoto.Record, error) {
	return f.getRec, f.getErr
}

func (f *fakeService) Open(context.Context, corescope.Scope, string) (corephoto.Record, io.ReadCloser, error) {
	f.openCalls++
	return f.getRec, f.getBody, f.getErr
}

func (f *fakeService) Delete(_ context.Context, scope corescope.Scope, _ string) (corephoto.Record, error) {
	f.scopes = append(f.scopes, scope)
	return corephoto.Record{}, f.deleteErr
}

func (f *fakeService) SetRetention(_ context.Context, scope corescope.Scope, _ int) (int, error) {
	f.scopes = append(f.scopes, scope)
	return f.retentionDays, f.retentionErr
}

// ListAccount、GetSchoolDefault 只满足端口：
// 本文件的用例覆盖 /me 五端点与默认图写入；其余账号面由真库 e2e 覆盖。
func (f *fakeService) ListAccount(context.Context, corescope.Scope, string, string, int) (appphotos.Page, error) {
	return appphotos.Page{}, errs.ErrNotFound
}

func (f *fakeService) GetSchoolDefault(context.Context, corescope.Scope, string) (appphotos.SchoolDefaultPhoto, error) {
	return appphotos.SchoolDefaultPhoto{}, errs.ErrNotFound
}

func (f *fakeService) SetSchoolDefault(_ context.Context, scope corescope.Scope, _ string, _ *string, _ appaudit.Actor) (appphotos.SchoolDefaultPhoto, error) {
	f.setDefaultScope = scope
	return f.setDefaultOut, f.setDefaultErr
}

type fakeAuthorizer struct{ scope corescope.Scope }

func (f *fakeAuthorizer) Authorize(context.Context, coreauth.User) (corescope.Scope, appaudit.Actor, error) {
	return f.scope, appaudit.Actor{}, nil
}

type fakeIssuer struct {
	secret []byte
	token  string
	sess   coreauth.Session
}

func (f *fakeIssuer) Issue(_ context.Context, user coreauth.User) (coreauth.Issued, error) {
	const token = "test-session-token"
	session := coreauth.Session{User: user, CSRFToken: coreauth.CSRFToken(f.secret, token)}
	f.token, f.sess = token, session
	return coreauth.Issued{Token: token, Session: session}, nil
}

func (f *fakeIssuer) Resolve(_ context.Context, token string) (coreauth.Session, error) {
	if token != f.token {
		return coreauth.Session{}, errors.New("unknown session")
	}
	return f.sess, nil
}

func (f *fakeIssuer) Revoke(context.Context, string) error           { return nil }
func (f *fakeIssuer) RevokeAllForUser(context.Context, string) error { return nil }

// ---- 辅助 ----

func newRouter(t *testing.T, scope corescope.Scope, svc photoshttp.Service) (http.Handler, coreauth.Issued) {
	t.Helper()
	return newRouterAs(t, scope, coreauth.RoleUser, svc)
}

// newRouterAs 按给定会话角色构造路由。
// /api/me/* 的归属取会话 userID，角色只影响 admin 的跨属主读取面。
func newRouterAs(t *testing.T, scope corescope.Scope, role coreauth.Role, svc photoshttp.Service) (http.Handler, coreauth.Issued) {
	t.Helper()
	issuer := &fakeIssuer{secret: []byte("0123456789abcdef0123456789abcdef")}
	issued, err := issuer.Issue(context.Background(), coreauth.User{
		ID: "u1", Email: "user@example.com", Role: role, Status: coreauth.StatusActive,
	})
	if err != nil {
		t.Fatalf("issue session: %v", err)
	}
	engine, err := router.New(router.Options{
		Sessions: issuer, InsecureCookies: true,
		Photos: photoshttp.NewHandler(svc, &fakeAuthorizer{scope: scope}),
	})
	if err != nil {
		t.Fatalf("router: %v", err)
	}
	return engine, issued
}

func request(t *testing.T, engine http.Handler, issued coreauth.Issued, method, path string, body io.Reader, contentType string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(method, path, body)
	req.AddCookie(&http.Cookie{Name: coreauth.InsecureCookieName, Value: issued.Token})
	req.Header.Set("X-CSRF-Token", issued.CSRFToken)
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	rec := httptest.NewRecorder()
	engine.ServeHTTP(rec, req)
	return rec
}

func requestWithHeaders(t *testing.T, engine http.Handler, issued coreauth.Issued,
	method, path string, headers map[string]string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(method, path, nil)
	req.AddCookie(&http.Cookie{Name: coreauth.InsecureCookieName, Value: issued.Token})
	req.Header.Set("X-CSRF-Token", issued.CSRFToken)
	for name, value := range headers {
		req.Header.Set(name, value)
	}
	rec := httptest.NewRecorder()
	engine.ServeHTTP(rec, req)
	return rec
}

// requestWithHeaderLines 逐行追加同名请求头。
// net/http 把每行存成独立条目，不按逗号拆分。
func requestWithHeaderLines(t *testing.T, engine http.Handler, issued coreauth.Issued,
	method, path, name string, values ...string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(method, path, nil)
	req.AddCookie(&http.Cookie{Name: coreauth.InsecureCookieName, Value: issued.Token})
	req.Header.Set("X-CSRF-Token", issued.CSRFToken)
	for _, value := range values {
		req.Header.Add(name, value)
	}
	rec := httptest.NewRecorder()
	engine.ServeHTTP(rec, req)
	return rec
}

// 7x2-3：If-None-Match 命中回 304（无体、保留 ETag）；弱比较/列表/* 均认，
// 不匹配与缺头回 200 带体。304 不声明 Content-Type 与 Content-Length。
func TestPhotoGetNotModified(t *testing.T) {
	data := []byte("png-bytes")
	head := sampleRecord()
	etag := `"` + hex.EncodeToString(head.SHA256[:]) + `"`
	cases := []struct {
		name string
		inm  string
		want int
	}{
		{"exact", etag, http.StatusNotModified},
		{"weak", "W/" + etag, http.StatusNotModified},
		{"list", `"other", ` + etag, http.StatusNotModified},
		{"weak in list", `"other", W/` + etag, http.StatusNotModified},
		{"star", "*", http.StatusNotModified},
		{"mismatch", `"other"`, http.StatusOK},
		{"absent", "", http.StatusOK},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			svc := &fakeService{getRec: recordFor(data), getBody: io.NopCloser(bytes.NewReader(data))}
			engine, issued := newRouter(t, corescope.User("u1"), svc)
			headers := map[string]string{}
			if tc.inm != "" {
				headers["If-None-Match"] = tc.inm
			}
			rec := requestWithHeaders(t, engine, issued, http.MethodGet, "/api/me/photos/photo-1", headers)
			if rec.Code != tc.want {
				t.Fatalf("status = %d, want %d: %s", rec.Code, tc.want, rec.Body.String())
			}
			if got := rec.Header().Get("ETag"); got != etag {
				t.Fatalf("etag = %q, want %q", got, etag)
			}
			if tc.want == http.StatusNotModified {
				if rec.Body.Len() != 0 {
					t.Fatalf("304 不得带体：%q", rec.Body.Bytes())
				}
				if ct := rec.Header().Get("Content-Type"); ct != "" {
					t.Fatalf("304 不应声明 Content-Type：%q", ct)
				}
				if cl := rec.Header().Get("Content-Length"); cl != "" {
					t.Fatalf("304 不应声明 Content-Length：%q", cl)
				}
				return
			}
			if !bytes.Equal(rec.Body.Bytes(), data) {
				t.Fatalf("200 body = %q", rec.Body.Bytes())
			}
		})
	}
}

// 条件命中只读记录，不打开 blob 流；未命中必须开流并回字节。
func TestPhotoGetNotModifiedSkipsBlobOpen(t *testing.T) {
	data := []byte("png-bytes")
	head := sampleRecord()
	etag := `"` + hex.EncodeToString(head.SHA256[:]) + `"`

	svc := &fakeService{getRec: recordFor(data), getBody: io.NopCloser(bytes.NewReader(data))}
	engine, issued := newRouter(t, corescope.User("u1"), svc)
	rec := requestWithHeaders(t, engine, issued, http.MethodGet, "/api/me/photos/photo-1",
		map[string]string{"If-None-Match": etag})
	if rec.Code != http.StatusNotModified {
		t.Fatalf("status = %d: %s", rec.Code, rec.Body.String())
	}
	if svc.openCalls != 0 {
		t.Fatalf("304 打开了 blob 流 %d 次；条件命中不得 Open", svc.openCalls)
	}

	svc = &fakeService{getRec: recordFor(data), getBody: io.NopCloser(bytes.NewReader(data))}
	engine, issued = newRouter(t, corescope.User("u1"), svc)
	rec = request(t, engine, issued, http.MethodGet, "/api/me/photos/photo-1", nil, "")
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d: %s", rec.Code, rec.Body.String())
	}
	if svc.openCalls != 1 {
		t.Fatalf("200 打开了 blob 流 %d 次，want 1", svc.openCalls)
	}
	if !bytes.Equal(rec.Body.Bytes(), data) {
		t.Fatalf("200 body = %q", rec.Body.Bytes())
	}
}

// RFC 9110 §5.3：列表头多行等价于合并成一行逗号分隔值。
// 生成绑定把多行头当非法；入口层必须先合并，否则客户端拿到 400。
func TestPhotoGetRepeatedIfNoneMatchLines(t *testing.T) {
	data := []byte("png-bytes")
	head := sampleRecord()
	etag := `"` + hex.EncodeToString(head.SHA256[:]) + `"`
	cases := []struct {
		name  string
		lines []string
		want  int
	}{
		{"second line exact", []string{`"other"`, etag}, http.StatusNotModified},
		{"second line weak", []string{`"other"`, "W/" + etag}, http.StatusNotModified},
		{"no match", []string{`"a"`, `"b"`}, http.StatusOK},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			svc := &fakeService{getRec: recordFor(data), getBody: io.NopCloser(bytes.NewReader(data))}
			engine, issued := newRouter(t, corescope.User("u1"), svc)
			rec := requestWithHeaderLines(t, engine, issued, http.MethodGet, "/api/me/photos/photo-1",
				"If-None-Match", tc.lines...)
			if rec.Code != tc.want {
				t.Fatalf("status = %d, want %d: %s", rec.Code, tc.want, rec.Body.String())
			}
			if got := rec.Header().Get("ETag"); got != etag {
				t.Fatalf("etag = %q, want %q", got, etag)
			}
			if tc.want == http.StatusNotModified {
				if rec.Body.Len() != 0 {
					t.Fatalf("304 不得带体：%q", rec.Body.Bytes())
				}
				return
			}
			if !bytes.Equal(rec.Body.Bytes(), data) {
				t.Fatalf("200 body = %q", rec.Body.Bytes())
			}
		})
	}
}

func uploadBody(t *testing.T, field string, data []byte) (*bytes.Buffer, string) {
	t.Helper()
	var buf bytes.Buffer
	w := multipart.NewWriter(&buf)
	part, err := w.CreateFormFile(field, "photo.png")
	if err != nil {
		t.Fatalf("create part: %v", err)
	}
	if _, err := part.Write(data); err != nil {
		t.Fatalf("write part: %v", err)
	}
	if err := w.Close(); err != nil {
		t.Fatalf("close writer: %v", err)
	}
	return &buf, w.FormDataContentType()
}

func sampleRecord() corephoto.Record {
	var sha [32]byte
	copy(sha[:], []byte("0123456789abcdef0123456789abcdef"))
	return corephoto.Record{
		ID: "photo-1", OwnerUserID: "u1", SHA256: sha, Mime: "image/png",
		Width: 12, Height: 8, Bytes: 4, CreatedAt: time.Date(2026, 10, 27, 8, 0, 0, 0, time.UTC),
	}
}

// recordFor 返回字节数等于 data 的记录。
// 流式用例的 Content-Length 必须等于实际写出的字节数。
func recordFor(data []byte) corephoto.Record {
	rec := sampleRecord()
	rec.Bytes = int64(len(data))
	return rec
}

// ---- 用例 ----

// 列表返回条目与游标。
func TestPhotoList(t *testing.T) {
	svc := &fakeService{page: appphotos.Page{Items: []corephoto.Record{sampleRecord()}, NextCursor: "next"}}
	engine, issued := newRouter(t, corescope.User("u1"), svc)
	rec := request(t, engine, issued, http.MethodGet, "/api/me/photos", nil, "")
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d: %s", rec.Code, rec.Body.String())
	}
	var page oapi.PhotoPage
	if err := json.Unmarshal(rec.Body.Bytes(), &page); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(page.Items) != 1 || page.NextCursor != "next" {
		t.Fatalf("page = %+v", page)
	}
	want := sampleRecord()
	if page.Items[0].Sha256 != hex.EncodeToString(want.SHA256[:]) {
		t.Fatalf("sha = %q", page.Items[0].Sha256)
	}
}

// 上传：原图字节交给用例，响应 201 与图片元数据。
func TestPhotoUpload(t *testing.T) {
	svc := &fakeService{uploadRec: sampleRecord()}
	engine, issued := newRouter(t, corescope.User("u1"), svc)
	raw := []byte("fake image bytes")
	body, contentType := uploadBody(t, "image", raw)
	rec := request(t, engine, issued, http.MethodPost, "/api/me/photos", body, contentType)
	if rec.Code != http.StatusCreated {
		t.Fatalf("status = %d: %s", rec.Code, rec.Body.String())
	}
	if !bytes.Equal(svc.lastRaw, raw) {
		t.Fatalf("raw = %q, want %q", svc.lastRaw, raw)
	}
	var photo oapi.Photo
	if err := json.Unmarshal(rec.Body.Bytes(), &photo); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if photo.Id != "photo-1" || photo.Width != 12 {
		t.Fatalf("photo = %+v", photo)
	}
}

// 缺 image 字段与超限体积都回 validation，且不调用用例。
func TestPhotoUploadRejectsBadBody(t *testing.T) {
	svc := &fakeService{uploadRec: sampleRecord()}
	engine, issued := newRouter(t, corescope.User("u1"), svc)

	body, contentType := uploadBody(t, "other", []byte("x"))
	rec := request(t, engine, issued, http.MethodPost, "/api/me/photos", body, contentType)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("缺字段 status = %d: %s", rec.Code, rec.Body.String())
	}
	if svc.lastRaw != nil {
		t.Fatal("缺字段不应调用用例")
	}

	big := bytes.Repeat([]byte{'x'}, corephoto.MaxBytes+1)
	body, contentType = uploadBody(t, "image", big)
	rec = request(t, engine, issued, http.MethodPost, "/api/me/photos", body, contentType)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("超限 status = %d: %s", rec.Code, rec.Body.String())
	}
	if svc.lastRaw != nil {
		t.Fatal("超限不应调用用例")
	}
}

// 单图读取：真实媒体类型与 ETag，字节原样流出。
func TestPhotoGetStreams(t *testing.T) {
	data := []byte("png-bytes")
	svc := &fakeService{
		getRec:  recordFor(data),
		getBody: io.NopCloser(bytes.NewReader(data)),
	}
	engine, issued := newRouter(t, corescope.User("u1"), svc)
	rec := request(t, engine, issued, http.MethodGet, "/api/me/photos/photo-1", nil, "")
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d: %s", rec.Code, rec.Body.String())
	}
	if rec.Header().Get("Content-Type") != "image/png" {
		t.Fatalf("content-type = %q", rec.Header().Get("Content-Type"))
	}
	if got := rec.Header().Get("Content-Length"); got != strconv.Itoa(len(data)) {
		t.Fatalf("content-length = %q, want %d", got, len(data))
	}
	head := sampleRecord()
	wantETag := `"` + hex.EncodeToString(head.SHA256[:]) + `"`
	if rec.Header().Get("ETag") != wantETag {
		t.Fatalf("etag = %q, want %q", rec.Header().Get("ETag"), wantETag)
	}
	if !bytes.Equal(rec.Body.Bytes(), data) {
		t.Fatalf("body = %q", rec.Body.Bytes())
	}

	svc.getErr = errs.ErrNotFound
	rec = request(t, engine, issued, http.MethodGet, "/api/me/photos/photo-1", nil, "")
	if rec.Code != http.StatusNotFound {
		t.Fatalf("缺失 status = %d", rec.Code)
	}
}

// 删除 204；保留期返回闭集值。
func TestPhotoDeleteAndRetention(t *testing.T) {
	svc := &fakeService{retentionDays: 180}
	engine, issued := newRouter(t, corescope.User("u1"), svc)
	rec := request(t, engine, issued, http.MethodDelete, "/api/me/photos/photo-1", nil, "")
	if rec.Code != http.StatusNoContent {
		t.Fatalf("delete status = %d: %s", rec.Code, rec.Body.String())
	}

	body := strings.NewReader(`{"days":180}`)
	rec = request(t, engine, issued, http.MethodPut, "/api/me/photo-retention", body, "application/json")
	if rec.Code != http.StatusOK {
		t.Fatalf("retention status = %d: %s", rec.Code, rec.Body.String())
	}
	var out oapi.PhotoRetention
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if out.Days != 180 {
		t.Fatalf("days = %d", out.Days)
	}
}

// /api/me/* 的归属只看会话 userID：master、admin、user 都只读写自己的库。
func TestPhotoLibraryUsesOwnerScopeForEveryRole(t *testing.T) {
	cases := []struct {
		role  coreauth.Role
		scope corescope.Scope
	}{
		{coreauth.RoleMaster, corescope.Master()},
		{coreauth.RoleAdmin, corescope.Admin([]string{"s1"})},
		{coreauth.RoleUser, corescope.User("u1")},
	}
	for _, c := range cases {
		svc := &fakeService{uploadRec: sampleRecord(), retentionDays: 90}
		engine, issued := newRouterAs(t, c.scope, c.role, svc)
		body, contentType := uploadBody(t, "image", []byte("x"))
		if rec := request(t, engine, issued, http.MethodPost, "/api/me/photos", body, contentType); rec.Code != http.StatusCreated {
			t.Fatalf("%v upload status = %d: %s", c.role, rec.Code, rec.Body.String())
		}
		if rec := request(t, engine, issued, http.MethodGet, "/api/me/photos", nil, ""); rec.Code != http.StatusOK {
			t.Fatalf("%v list status = %d: %s", c.role, rec.Code, rec.Body.String())
		}
		if rec := request(t, engine, issued, http.MethodDelete, "/api/me/photos/photo-1", nil, ""); rec.Code != http.StatusNoContent {
			t.Fatalf("%v delete status = %d: %s", c.role, rec.Code, rec.Body.String())
		}
		rec := request(t, engine, issued, http.MethodPut, "/api/me/photo-retention",
			strings.NewReader(`{"days":180}`), "application/json")
		if rec.Code != http.StatusOK {
			t.Fatalf("%v retention status = %d: %s", c.role, rec.Code, rec.Body.String())
		}
		if len(svc.scopes) != 4 {
			t.Fatalf("%v service calls = %d, want 4", c.role, len(svc.scopes))
		}
		for _, scope := range svc.scopes {
			if scope.Kind() != corescope.KindUser || scope.UserID() != "u1" {
				t.Fatalf("%v scope = kind %v user %q, want owner u1", c.role, scope.Kind(), scope.UserID())
			}
		}
	}
}

// kindService 按作用域种类返回不同结果，用于钉住 admin 的跨属主读取面。
type kindService struct {
	*fakeService
	getErrByKind map[corescope.Kind]error
	kinds        []corescope.Kind
}

func (s *kindService) Get(_ context.Context, scope corescope.Scope, _ string) (corephoto.Record, error) {
	s.kinds = append(s.kinds, scope.Kind())
	if err, ok := s.getErrByKind[scope.Kind()]; ok {
		return corephoto.Record{}, err
	}
	return recordFor([]byte("png")), nil
}

func (s *kindService) Open(_ context.Context, scope corescope.Scope, _ string) (corephoto.Record, io.ReadCloser, error) {
	s.kinds = append(s.kinds, scope.Kind())
	return recordFor([]byte("png")), io.NopCloser(bytes.NewReader([]byte("png"))), nil
}

func assertKinds(t *testing.T, got []corescope.Kind, want ...corescope.Kind) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("scope kinds = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("scope kinds = %v, want %v", got, want)
		}
	}
}

// admin 的属主未命中时回落到绑定校范围；两步都按序发生。
func TestPhotoGetAdminFallsBackToBoundSchools(t *testing.T) {
	svc := &kindService{
		fakeService:  &fakeService{},
		getErrByKind: map[corescope.Kind]error{corescope.KindUser: errs.ErrNotFound},
	}
	engine, issued := newRouterAs(t, corescope.Admin([]string{"s1"}), coreauth.RoleAdmin, svc)
	rec := request(t, engine, issued, http.MethodGet, "/api/me/photos/photo-1", nil, "")
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d: %s", rec.Code, rec.Body.String())
	}
	assertKinds(t, svc.kinds, corescope.KindUser, corescope.KindAdmin, corescope.KindAdmin)
}

// master 没有跨属主读取面：属主未命中就是 404，不回落到 master 范围。
func TestPhotoGetMasterStaysOwnerOnly(t *testing.T) {
	svc := &kindService{
		fakeService:  &fakeService{},
		getErrByKind: map[corescope.Kind]error{corescope.KindUser: errs.ErrNotFound},
	}
	engine, issued := newRouterAs(t, corescope.Master(), coreauth.RoleMaster, svc)
	rec := request(t, engine, issued, http.MethodGet, "/api/me/photos/photo-1", nil, "")
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d: %s", rec.Code, rec.Body.String())
	}
	assertKinds(t, svc.kinds, corescope.KindUser)
}

// 学校缺省图写入走授权作用域（admin），不是 /me 的 owner 作用域。
func TestSchoolDefaultPhotoSetUsesAuthorizedScope(t *testing.T) {
	svc := &fakeService{setDefaultOut: appphotos.SchoolDefaultPhoto{PhotoID: "photo-1", ETag: "abc"}}
	engine, issued := newRouterAs(t, corescope.Admin([]string{"s1"}), coreauth.RoleAdmin, svc)
	body := strings.NewReader(`{"photo_id":"01900000-0000-7000-8000-000000000001"}`)
	rec := request(t, engine, issued, http.MethodPut, "/api/schools/s1/default-photo", body, "application/json")
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d: %s", rec.Code, rec.Body.String())
	}
	if svc.setDefaultScope.Kind() != corescope.KindAdmin {
		t.Fatalf("scope = %v, want admin", svc.setDefaultScope.Kind())
	}
}

// 上传完成后系统临时目录不得残留 multipart-*：
// 原始字节未去 EXIF，泄漏等于把隐私面留在磁盘上。
// 成功路径与「字段名不对」的早退路径都必须清理。
func TestPhotoUploadCleansTempFiles(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("TMPDIR", dir)
	t.Setenv("TMP", dir)
	t.Setenv("TEMP", dir)
	svc := &fakeService{uploadRec: sampleRecord()}
	engine, issued := newRouter(t, corescope.User("u1"), svc)
	for _, tc := range []struct {
		field string
		want  int
	}{
		{"image", http.StatusCreated},
		{"other", http.StatusBadRequest},
	} {
		// 2 MiB 超过 1 MiB 内存阈值，part 必然落盘。
		body, contentType := uploadBody(t, tc.field, bytes.Repeat([]byte{'x'}, 2<<20))
		rec := request(t, engine, issued, http.MethodPost, "/api/me/photos", body, contentType)
		if rec.Code != tc.want {
			t.Fatalf("field %q status = %d, want %d: %s", tc.field, rec.Code, tc.want, rec.Body.String())
		}
		entries, err := os.ReadDir(dir)
		if err != nil {
			t.Fatalf("read tmpdir: %v", err)
		}
		for _, entry := range entries {
			if strings.HasPrefix(entry.Name(), "multipart-") {
				t.Fatalf("field %q 临时文件残留: %s", tc.field, entry.Name())
			}
		}
	}
}

// ---- 路由级 deadline ----
//
// 全局 http.Server 的 ReadTimeout/WriteTimeout 从读到请求头起算，
// 10 MiB 上传与慢链路下载都会越过它。下列用例在真实 TCP 端口上
// 用 100ms 全局超时钉住路由级放宽；没有放宽时连接被切断。

// serveWithTimeouts 在真实端口上服务路由，全局读写超时都很短。
func serveWithTimeouts(t *testing.T, engine http.Handler, read, write time.Duration) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	srv := &http.Server{Handler: engine, ReadTimeout: read, WriteTimeout: write}
	go func() { _ = srv.Serve(ln) }()
	t.Cleanup(func() { _ = srv.Close() })
	return "http://" + ln.Addr().String()
}

// authenticate 给请求带上会话 cookie 与 CSRF 头。
func authenticate(req *http.Request, issued coreauth.Issued) {
	req.AddCookie(&http.Cookie{Name: coreauth.InsecureCookieName, Value: issued.Token})
	req.Header.Set("X-CSRF-Token", issued.CSRFToken)
}

// 慢上传：请求体分 5 段、每段间隔 40ms，总时长超过全局 ReadTimeout。
// 期望仍拿到 201 与完整原始字节。
func TestPhotoUploadSurvivesServerReadTimeout(t *testing.T) {
	svc := &fakeService{uploadRec: sampleRecord()}
	engine, issued := newRouter(t, corescope.User("u1"), svc)
	baseURL := serveWithTimeouts(t, engine, 100*time.Millisecond, 100*time.Millisecond)

	raw := bytes.Repeat([]byte{'x'}, 4<<10)
	full, contentType := uploadBody(t, "image", raw)
	pr, pw := io.Pipe()
	defer pr.Close()
	go func() {
		defer pw.Close()
		data := full.Bytes()
		chunk := (len(data) + 4) / 5
		for start := 0; start < len(data); start += chunk {
			end := start + chunk
			if end > len(data) {
				end = len(data)
			}
			if _, err := pw.Write(data[start:end]); err != nil {
				return
			}
			time.Sleep(40 * time.Millisecond)
		}
	}()

	req, err := http.NewRequest(http.MethodPost, baseURL+"/api/me/photos", pr)
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	req.Header.Set("Content-Type", contentType)
	authenticate(req, issued)
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("慢上传被切断: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusCreated {
		body, _ := io.ReadAll(resp.Body)
		t.Fatalf("status = %d: %s", resp.StatusCode, body)
	}
	if !bytes.Equal(svc.lastRaw, raw) {
		t.Fatalf("上传字节不完整: %d, want %d", len(svc.lastRaw), len(raw))
	}
}

// slowReader 每 chunk 字节产出一次，间隔 delay：
// 模拟磁盘或慢链路的写入节奏。
type slowReader struct {
	data  []byte
	chunk int
	delay time.Duration
}

func (r *slowReader) Read(p []byte) (int, error) {
	if len(r.data) == 0 {
		return 0, io.EOF
	}
	n := r.chunk
	if n > len(p) {
		n = len(p)
	}
	if n > len(r.data) {
		n = len(r.data)
	}
	copy(p, r.data[:n])
	r.data = r.data[n:]
	if len(r.data) > 0 {
		time.Sleep(r.delay)
	}
	return n, nil
}

// 慢下载：字节以每 1KiB / 20ms 的节奏产出，总时长超过全局 WriteTimeout。
// 期望状态 200 且读回全部字节。
func TestPhotoGetSurvivesServerWriteTimeout(t *testing.T) {
	data := bytes.Repeat([]byte("png"), 4<<10)
	svc := &fakeService{
		getRec:  recordFor(data),
		getBody: io.NopCloser(&slowReader{data: data, chunk: 1 << 10, delay: 20 * time.Millisecond}),
	}
	engine, issued := newRouter(t, corescope.User("u1"), svc)
	baseURL := serveWithTimeouts(t, engine, 100*time.Millisecond, 100*time.Millisecond)

	req, err := http.NewRequest(http.MethodGet, baseURL+"/api/me/photos/photo-1", nil)
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	authenticate(req, issued)
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("慢下载被切断: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	got, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("读取被切断: %v", err)
	}
	if !bytes.Equal(got, data) {
		t.Fatalf("字节不完整: %d, want %d", len(got), len(data))
	}
}
