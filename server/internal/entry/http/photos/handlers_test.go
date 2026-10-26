package photos_test

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"os"
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

	deleteErr error

	retentionDays int
	retentionErr  error
}

func (f *fakeService) Upload(_ context.Context, scope corescope.Scope, raw []byte) (corephoto.Record, error) {
	if scope.Kind() != corescope.KindUser {
		return corephoto.Record{}, errs.New(errs.CodeForbidden, "photo library is for user accounts")
	}
	f.lastRaw = append([]byte(nil), raw...)
	return f.uploadRec, f.uploadErr
}

func (f *fakeService) List(context.Context, corescope.Scope, string, int) (appphotos.Page, error) {
	return f.page, f.pageErr
}

func (f *fakeService) Get(context.Context, corescope.Scope, string) (corephoto.Record, error) {
	return f.getRec, f.getErr
}

func (f *fakeService) Open(context.Context, corescope.Scope, string) (corephoto.Record, io.ReadCloser, error) {
	return f.getRec, f.getBody, f.getErr
}

func (f *fakeService) Delete(context.Context, corescope.Scope, string) (corephoto.Record, error) {
	return corephoto.Record{}, f.deleteErr
}

func (f *fakeService) SetRetention(context.Context, corescope.Scope, int) (int, error) {
	return f.retentionDays, f.retentionErr
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

func newRouter(t *testing.T, scope corescope.Scope, svc *fakeService) (http.Handler, coreauth.Issued) {
	t.Helper()
	issuer := &fakeIssuer{secret: []byte("0123456789abcdef0123456789abcdef")}
	issued, err := issuer.Issue(context.Background(), coreauth.User{
		ID: "u1", Email: "user@example.com", Role: coreauth.RoleUser, Status: coreauth.StatusActive,
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
		getRec:  sampleRecord(),
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

// admin 上传被用例拒绝时映射 403。
func TestPhotoUploadAdminForbidden(t *testing.T) {
	svc := &fakeService{uploadRec: sampleRecord()}
	engine, issued := newRouter(t, corescope.Admin([]string{"s1"}), svc)
	body, contentType := uploadBody(t, "image", []byte("x"))
	rec := request(t, engine, issued, http.MethodPost, "/api/me/photos", body, contentType)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d: %s", rec.Code, rec.Body.String())
	}
}

// 上传完成后系统临时目录不得残留 multipart-*：
// 原始字节未去 EXIF，泄漏等于把隐私面留在磁盘上。
func TestPhotoUploadCleansTempFiles(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("TMPDIR", dir)
	t.Setenv("TMP", dir)
	t.Setenv("TEMP", dir)
	svc := &fakeService{uploadRec: sampleRecord()}
	engine, issued := newRouter(t, corescope.User("u1"), svc)
	// 2 MiB 超过 1 MiB 内存阈值，part 必然落盘。
	body, contentType := uploadBody(t, "image", bytes.Repeat([]byte{'x'}, 2<<20))
	rec := request(t, engine, issued, http.MethodPost, "/api/me/photos", body, contentType)
	if rec.Code != http.StatusCreated {
		t.Fatalf("status = %d: %s", rec.Code, rec.Body.String())
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("read tmpdir: %v", err)
	}
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), "multipart-") {
			t.Fatalf("临时文件残留: %s", entry.Name())
		}
	}
}
