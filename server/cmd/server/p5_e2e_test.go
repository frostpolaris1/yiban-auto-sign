package main

// P5-B e2e：图片库五个端点。
//
// # 功能
// 用真实 PostgreSQL 与临时 blob 目录走完整链路：
// 上传、去重、列表、流式读取、保留期、软删与作用域拒绝。
//
// # 归属
// cmd/server 的 e2e。复用 w2cEnv 的 schema 隔离与 HTTP 客户端。
//
// # 口径
// blob 目录是 t.TempDir()，随测试回收。
// 不测平台上传协议：本切片没有协议实现。

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"image"
	"image/color"
	"image/png"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"testing"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
)

// pngPhoto 生成一张可编码的纯色 PNG。
func pngPhoto(t *testing.T, w, h int) []byte {
	t.Helper()
	img := image.NewNRGBA(image.Rect(0, 0, w, h))
	for y := 0; y < h; y++ {
		for x := 0; x < w; x++ {
			img.SetNRGBA(x, y, color.NRGBA{R: 30, G: 60, B: 90, A: 255})
		}
	}
	var buf bytes.Buffer
	if err := png.Encode(&buf, img); err != nil {
		t.Fatalf("encode png: %v", err)
	}
	return buf.Bytes()
}

// uploadPhoto 发一次 multipart 上传。
func (e *w2cEnv) uploadPhoto(issued *coreauth.Issued, raw []byte) *httptest.ResponseRecorder {
	e.t.Helper()
	var buf bytes.Buffer
	w := multipart.NewWriter(&buf)
	part, err := w.CreateFormFile("image", "photo.png")
	if err != nil {
		e.t.Fatalf("create part: %v", err)
	}
	if _, err := part.Write(raw); err != nil {
		e.t.Fatalf("write part: %v", err)
	}
	if err := w.Close(); err != nil {
		e.t.Fatalf("close writer: %v", err)
	}
	req := httptest.NewRequest(http.MethodPost, "/api/me/photos", &buf)
	req.Header.Set("Content-Type", w.FormDataContentType())
	req.AddCookie(&http.Cookie{Name: coreauth.InsecureCookieName, Value: issued.Token})
	req.Header.Set("X-CSRF-Token", issued.CSRFToken)
	rec := httptest.NewRecorder()
	e.engine.ServeHTTP(rec, req)
	return rec
}

func TestP5PhotoLibrary(t *testing.T) {
	e := w2cNewEnv(t)
	owner := e.user("p5-photo-owner@example.com", coreauth.RoleUser)
	issued := e.login(owner)
	other := e.user("p5-photo-other@example.com", coreauth.RoleUser)
	otherIssued := e.login(other)
	admin := e.user("p5-photo-admin@example.com", coreauth.RoleAdmin)
	adminIssued := e.login(admin)

	raw := pngPhoto(t, 12, 8)
	rec := e.uploadPhoto(issued, raw)
	if rec.Code != http.StatusCreated {
		t.Fatalf("upload status = %d: %s", rec.Code, rec.Body.String())
	}
	var photo oapi.Photo
	if err := json.Unmarshal(rec.Body.Bytes(), &photo); err != nil {
		t.Fatalf("decode photo: %v", err)
	}
	if photo.Width != 12 || photo.Height != 8 || photo.Mime != "image/png" || photo.Sha256 == "" {
		t.Fatalf("photo = %+v", photo)
	}

	// 同字节重传：同一 id。
	rec = e.uploadPhoto(issued, raw)
	var again oapi.Photo
	if err := json.Unmarshal(rec.Body.Bytes(), &again); err != nil {
		t.Fatalf("decode again: %v", err)
	}
	if rec.Code != http.StatusCreated || again.Id != photo.Id {
		t.Fatalf("去重失败: %d %+v", rec.Code, again)
	}

	// 列表。
	rec = e.do("GET", "/api/me/photos", nil, issued)
	if rec.Code != http.StatusOK {
		t.Fatalf("list status = %d: %s", rec.Code, rec.Body.String())
	}
	var page oapi.PhotoPage
	if err := json.Unmarshal(rec.Body.Bytes(), &page); err != nil {
		t.Fatalf("decode page: %v", err)
	}
	if len(page.Items) != 1 || page.Items[0].Id != photo.Id {
		t.Fatalf("page = %+v", page)
	}

	// 流式读取：媒体类型、ETag 与字节。
	rec = e.do("GET", "/api/me/photos/"+photo.Id, nil, issued)
	if rec.Code != http.StatusOK {
		t.Fatalf("get status = %d: %s", rec.Code, rec.Body.String())
	}
	if rec.Header().Get("Content-Type") != "image/png" {
		t.Fatalf("content-type = %q", rec.Header().Get("Content-Type"))
	}
	wantETag := `"` + photo.Sha256 + `"`
	if rec.Header().Get("ETag") != wantETag {
		t.Fatalf("etag = %q, want %q", rec.Header().Get("ETag"), wantETag)
	}
	if rec.Body.Len() == 0 {
		t.Fatal("图片字节为空")
	}
	if _, err := hex.DecodeString(photo.Sha256); err != nil {
		t.Fatalf("sha 不是 hex: %v", err)
	}

	// 作用域：他人 404；admin 读取 404、列表 403、上传 403。
	if rec = e.do("GET", "/api/me/photos/"+photo.Id, nil, otherIssued); rec.Code != http.StatusNotFound {
		t.Fatalf("他人读取 status = %d", rec.Code)
	}
	if rec = e.do("GET", "/api/me/photos/"+photo.Id, nil, adminIssued); rec.Code != http.StatusNotFound {
		t.Fatalf("admin 读取 status = %d", rec.Code)
	}
	if rec = e.do("GET", "/api/me/photos", nil, adminIssued); rec.Code != http.StatusForbidden {
		t.Fatalf("admin 列表 status = %d", rec.Code)
	}
	if rec = e.uploadPhoto(adminIssued, raw); rec.Code != http.StatusForbidden {
		t.Fatalf("admin 上传 status = %d", rec.Code)
	}

	// 保留期。
	rec = e.do("PUT", "/api/me/photo-retention", map[string]int{"days": 180}, issued)
	if rec.Code != http.StatusOK {
		t.Fatalf("retention status = %d: %s", rec.Code, rec.Body.String())
	}
	var ret oapi.PhotoRetention
	if err := json.Unmarshal(rec.Body.Bytes(), &ret); err != nil {
		t.Fatalf("decode retention: %v", err)
	}
	if ret.Days != 180 {
		t.Fatalf("days = %d", ret.Days)
	}

	// 软删：204、再读 404、列表清空；重传恢复同一 id。
	if rec = e.do("DELETE", "/api/me/photos/"+photo.Id, nil, issued); rec.Code != http.StatusNoContent {
		t.Fatalf("delete status = %d: %s", rec.Code, rec.Body.String())
	}
	if rec = e.do("GET", "/api/me/photos/"+photo.Id, nil, issued); rec.Code != http.StatusNotFound {
		t.Fatalf("删除后读取 status = %d", rec.Code)
	}
	rec = e.do("GET", "/api/me/photos", nil, issued)
	page = oapi.PhotoPage{}
	if err := json.Unmarshal(rec.Body.Bytes(), &page); err != nil {
		t.Fatalf("decode empty page: %v", err)
	}
	if len(page.Items) != 0 {
		t.Fatalf("删除后列表 = %d 行", len(page.Items))
	}
	restored := e.uploadPhoto(issued, raw)
	var back oapi.Photo
	if err := json.Unmarshal(restored.Body.Bytes(), &back); err != nil {
		t.Fatalf("decode restored: %v", err)
	}
	if restored.Code != http.StatusCreated || back.Id != photo.Id {
		t.Fatalf("未恢复同一行: %d %+v", restored.Code, back)
	}
}
