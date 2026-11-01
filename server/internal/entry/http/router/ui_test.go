package router_test

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/router"
)

// uiIndexMarker 与 uiAssetMarker 让断言只认本次临时目录的内容。
const (
	uiIndexMarker = "ui-index-marker"
	uiAssetMarker = "ui-asset-marker"
)

// writeUIDist 造一个最小的构建产物目录。
func writeUIDist(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "index.html"),
		[]byte("<!doctype html><div id=\"root\">"+uiIndexMarker+"</div>"), 0o644); err != nil {
		t.Fatalf("写 index.html: %v", err)
	}
	if err := os.MkdirAll(filepath.Join(dir, "assets"), 0o755); err != nil {
		t.Fatalf("建 assets: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "assets", "app-abc123.js"),
		[]byte("// "+uiAssetMarker+"\n"), 0o644); err != nil {
		t.Fatalf("写 asset: %v", err)
	}
	return dir
}

func newUIEngine(t *testing.T, dir string) http.Handler {
	t.Helper()
	engine, err := router.New(router.Options{Sessions: newStubIssuer(), UIDir: dir})
	if err != nil {
		t.Fatalf("router.New: %v", err)
	}
	return engine
}

func get(t *testing.T, h http.Handler, target string) *httptest.ResponseRecorder {
	t.Helper()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, target, nil))
	return rec
}

func TestUIServesIndexAtRoot(t *testing.T) {
	h := newUIEngine(t, writeUIDist(t))

	rec := get(t, h, "/")
	if rec.Code != http.StatusOK {
		t.Fatalf("GET / = %d, want 200", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), uiIndexMarker) {
		t.Fatalf("GET / 未返回 index.html: %q", rec.Body.String())
	}
	if got := rec.Header().Get("Cache-Control"); got != "no-cache" {
		t.Fatalf("index Cache-Control = %q, want no-cache", got)
	}
	if got := rec.Header().Get("Content-Type"); !strings.HasPrefix(got, "text/html") {
		t.Fatalf("index Content-Type = %q, want text/html", got)
	}
}

// 前端深链由页面路由接管：服务端回 index.html，不是 404。
func TestUIServesSPAFallbackForClientRoutes(t *testing.T) {
	h := newUIEngine(t, writeUIDist(t))
	for _, target := range []string{"/accounts", "/users", "/login?redirect=%2Faccounts", "/schedule?day=2026-11-01"} {
		rec := get(t, h, target)
		if rec.Code != http.StatusOK {
			t.Errorf("GET %s = %d, want 200", target, rec.Code)
			continue
		}
		if !strings.Contains(rec.Body.String(), uiIndexMarker) {
			t.Errorf("GET %s 未回 index.html: %q", target, rec.Body.String())
		}
	}
}

func TestUIServesHashedAssetWithLongCache(t *testing.T) {
	h := newUIEngine(t, writeUIDist(t))

	rec := get(t, h, "/assets/app-abc123.js")
	if rec.Code != http.StatusOK {
		t.Fatalf("GET asset = %d, want 200", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), uiAssetMarker) {
		t.Fatalf("asset 内容不对: %q", rec.Body.String())
	}
	if got := rec.Header().Get("Cache-Control"); got != "public, max-age=31536000, immutable" {
		t.Fatalf("asset Cache-Control = %q", got)
	}
	if got := rec.Header().Get("Content-Type"); !strings.Contains(got, "javascript") {
		t.Fatalf("asset Content-Type = %q", got)
	}
}

// 缺资产不得回 index：否则浏览器拿到 HTML 与 200，错误被推迟到运行期。
func TestUIMissingAssetIsJSON404(t *testing.T) {
	h := newUIEngine(t, writeUIDist(t))

	rec := get(t, h, "/assets/missing-xyz.js")
	if rec.Code != http.StatusNotFound {
		t.Fatalf("GET 缺失资产 = %d, want 404", rec.Code)
	}
	assertNotFoundEnvelope(t, rec)
}

// API 的未知路径必须保持 JSON 404，不得被页面路由吞掉。
func TestUIKeepsAPI404JSON(t *testing.T) {
	h := newUIEngine(t, writeUIDist(t))
	for _, target := range []string{"/api", "/api/unknown", "/api/definitely-not-a-route"} {
		rec := get(t, h, target)
		if rec.Code != http.StatusNotFound {
			t.Errorf("GET %s = %d, want 404", target, rec.Code)
			continue
		}
		assertNotFoundEnvelope(t, rec)
	}
}

// .. 不得逃出根目录。
func TestUIDoesNotEscapeRoot(t *testing.T) {
	dir := writeUIDist(t)
	parent := filepath.Dir(dir)
	const secret = "outside-secret-marker"
	if err := os.WriteFile(filepath.Join(parent, "secret.txt"), []byte(secret), 0o644); err != nil {
		t.Fatalf("写目录外文件: %v", err)
	}
	t.Cleanup(func() { _ = os.Remove(filepath.Join(parent, "secret.txt")) })

	h := newUIEngine(t, dir)
	for _, target := range []string{"/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt"} {
		req := httptest.NewRequest(http.MethodGet, "/", nil)
		req.URL.Path = target
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if strings.Contains(rec.Body.String(), secret) {
			t.Errorf("GET %s 读到了目录外文件", target)
			continue
		}
		if rec.Code != http.StatusNotFound {
			t.Errorf("GET %s = %d, want 404", target, rec.Code)
		}
	}
}

func TestUIHeadHasNoBody(t *testing.T) {
	h := newUIEngine(t, writeUIDist(t))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodHead, "/", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("HEAD / = %d, want 200", rec.Code)
	}
	if rec.Body.Len() != 0 {
		t.Fatalf("HEAD / 有 body %d 字节", rec.Body.Len())
	}
}

// 目录设置错误必须启动失败：不带病上线。
func TestUINewFailsWithoutIndex(t *testing.T) {
	dir := t.TempDir()
	if _, err := router.New(router.Options{Sessions: newStubIssuer(), UIDir: dir}); err == nil {
		t.Fatal("缺 index.html 时 router.New 仍成功")
	}
	if _, err := router.New(router.Options{Sessions: newStubIssuer(), UIDir: filepath.Join(dir, "missing")}); err == nil {
		t.Fatal("目录不存在时 router.New 仍成功")
	}
	// index.html 是目录同样不是可用的构建产物。
	if err := os.Mkdir(filepath.Join(dir, "index.html"), 0o755); err != nil {
		t.Fatalf("建目录 index.html: %v", err)
	}
	if _, err := router.New(router.Options{Sessions: newStubIssuer(), UIDir: dir}); err == nil {
		t.Fatal("index.html 是目录时 router.New 仍成功")
	}
}

// os.Root 必须挡住指向根目录外的符号链接。
func TestUIDoesNotFollowEscapingSymlink(t *testing.T) {
	dir := writeUIDist(t)
	outside := filepath.Join(filepath.Dir(dir), "outside-link-target.txt")
	const secret = "symlink-secret-marker"
	if err := os.WriteFile(outside, []byte(secret), 0o644); err != nil {
		t.Fatalf("写目录外文件: %v", err)
	}
	t.Cleanup(func() { _ = os.Remove(outside) })
	if err := os.Symlink(outside, filepath.Join(dir, "leak.txt")); err != nil {
		t.Skipf("SKIP: 当前平台不能建符号链接: %v", err)
	}

	h := newUIEngine(t, dir)
	rec := get(t, h, "/leak.txt")
	if strings.Contains(rec.Body.String(), secret) {
		t.Fatal("符号链接读到了目录外文件")
	}
	if rec.Code != http.StatusNotFound {
		t.Fatalf("GET /leak.txt = %d, want 404", rec.Code)
	}
}

// /api 精确路径的 404 也不得被缓存。
func TestAPIExactPathIsNotCached(t *testing.T) {
	h := newUIEngine(t, writeUIDist(t))
	rec := get(t, h, "/api")
	if rec.Code != http.StatusNotFound {
		t.Fatalf("GET /api = %d, want 404", rec.Code)
	}
	if got := rec.Header().Get("Cache-Control"); got != "no-store" {
		t.Fatalf("/api Cache-Control = %q, want no-store", got)
	}
}

// 未设置 YIBAN_UI_DIR 时行为不变：根路径仍是 JSON 404。
func TestUIOffKeepsJSON404(t *testing.T) {
	engine, err := router.New(router.Options{Sessions: newStubIssuer()})
	if err != nil {
		t.Fatalf("router.New: %v", err)
	}
	rec := get(t, engine, "/")
	if rec.Code != http.StatusNotFound {
		t.Fatalf("GET / = %d, want 404", rec.Code)
	}
	assertNotFoundEnvelope(t, rec)
}

func assertNotFoundEnvelope(t *testing.T, rec *httptest.ResponseRecorder) {
	t.Helper()
	var envelope errorEnvelope
	if err := json.Unmarshal(rec.Body.Bytes(), &envelope); err != nil {
		t.Fatalf("404 body 不是 JSON 信封: %v (%q)", err, rec.Body.String())
	}
	if envelope.Error.Code != string(errs.CodeNotFound) {
		t.Fatalf("404 code = %q, want %q", envelope.Error.Code, errs.CodeNotFound)
	}
}
