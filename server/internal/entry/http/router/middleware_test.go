package router

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	corephoto "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/photo"
)

// 图片上传路径的请求体上界必须容纳 10 MiB 原图加 multipart 头；
// 其余路径仍是 1 MiB，避免公开 POST 被大体积拖住。
func TestRequestBodyLimitPerPath(t *testing.T) {
	if maxPhotoUploadBytes < corephoto.MaxBytes+1<<20 {
		t.Fatalf("上传上限 %d 不足以容纳 %d 字节原图", maxPhotoUploadBytes, corephoto.MaxBytes)
	}
	cases := []struct {
		method, path string
		want         int64
	}{
		{"POST", "/api/me/photos", maxPhotoUploadBytes},
		{"GET", "/api/me/photos", maxRequestBodyBytes},
		{"POST", "/api/auth/login", maxRequestBodyBytes},
		{"PUT", "/api/me/photo-retention", maxRequestBodyBytes},
	}
	for _, tc := range cases {
		ctx, _ := gin.CreateTestContext(httptest.NewRecorder())
		ctx.Request = httptest.NewRequest(tc.method, tc.path, nil)
		if got := requestBodyLimit(ctx); got != tc.want {
			t.Fatalf("%s %s = %d, want %d", tc.method, tc.path, got, tc.want)
		}
	}
}

// 批量签到必须在 IP 限流白名单内：契约对整批声明 429，
// 白名单漏行＝429 运行时不可达。直接断言注册表，漏行即红。
func TestBatchSignPathIsIPRateLimited(t *testing.T) {
	for _, path := range []string{
		"/api/accounts/:id/sign",
		"/api/accounts/batch/sign",
	} {
		if _, ok := rateLimitedPaths[path]; !ok {
			t.Fatalf("%s 不在 rateLimitedPaths 白名单内", path)
		}
	}
	if len(rateLimitedPaths) < 7 {
		t.Fatalf("白名单 = %d 项, want at least 7（表被清空即红）", len(rateLimitedPaths))
	}
}

// cspWiringIssuer 是只满足 router.New 会话依赖的最小替身。
// 本文件不断言会话行为；解析一律失败即可。
type cspWiringIssuer struct{}

func (cspWiringIssuer) Issue(context.Context, coreauth.User) (coreauth.Issued, error) {
	return coreauth.Issued{}, nil
}
func (cspWiringIssuer) Resolve(context.Context, string) (coreauth.Session, error) {
	return coreauth.Session{}, coreauth.ErrNoSession
}
func (cspWiringIssuer) Revoke(context.Context, string) error           { return nil }
func (cspWiringIssuer) RevokeAllForUser(context.Context, string) error { return nil }

// 安全头必须由 router.New 的真实中间件链下发：删掉 router.go 的
// securityHeaders() 注册，本测试即红。CSP 的 script-src 不含
// 'unsafe-inline'，与 index.html 无内联块的结构守卫配套。
func TestSecurityHeadersWiredThroughRouter(t *testing.T) {
	gin.SetMode(gin.TestMode)
	engine, err := New(Options{Sessions: cspWiringIssuer{}})
	if err != nil {
		t.Fatalf("router.New: %v", err)
	}

	rec := httptest.NewRecorder()
	engine.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("GET /healthz = %d, want 200", rec.Code)
	}

	csp := rec.Header().Get("Content-Security-Policy")
	for _, want := range []string{
		"default-src 'self'",
		"script-src 'self'",
		"style-src 'self' 'unsafe-inline'",
		"img-src 'self' data:",
		"frame-ancestors 'none'",
		"object-src 'none'",
		"base-uri 'none'",
	} {
		if !strings.Contains(csp, want) {
			t.Fatalf("CSP 缺指令 %q（当前: %q）", want, csp)
		}
	}
	if strings.Contains(csp, "script-src 'self' 'unsafe-inline'") ||
		strings.Contains(csp, "script-src 'unsafe-inline'") {
		t.Fatalf("script-src 不得含 'unsafe-inline'（当前: %q）", csp)
	}
}

// index.html 不得有内联 <script> 块：CSP 的 script-src 只有 'self'，
// 内联块会被浏览器拦掉。主题引导必须外置且同步加载（防首屏闪色）。
func TestUIIndexHasNoInlineScripts(t *testing.T) {
	root := filepath.Join("..", "..", "..", "..", "..")
	indexPath := filepath.Join(root, "ui", "index.html")
	body, err := os.ReadFile(indexPath)
	if err != nil {
		t.Fatalf("读 %s: %v", indexPath, err)
	}
	scriptTag := regexp.MustCompile(`(?is)<script\b[^>]*>`)
	tags := scriptTag.FindAllString(string(body), -1)
	if len(tags) == 0 {
		t.Fatal("index.html 没有任何 <script> 标签：扫描面为空不得当绿")
	}
	for _, tag := range tags {
		if !strings.Contains(strings.ToLower(tag), "src=") {
			t.Fatalf("index.html 含内联 <script> 块，会被 CSP 拦掉: %s", tag)
		}
	}
	boot := filepath.Join(root, "ui", "public", "theme-boot.js")
	if _, err := os.Stat(boot); err != nil {
		t.Fatalf("主题引导脚本不存在: %v", err)
	}
	if !strings.Contains(string(body), `src="/theme-boot.js"`) {
		t.Fatal("index.html 没有引用 /theme-boot.js")
	}
	tag := scriptTag.FindString(string(body))
	if tag == "" || strings.Contains(strings.ToLower(tag), "defer") ||
		strings.Contains(strings.ToLower(tag), "async") {
		t.Fatalf("主题引导必须同步加载（无 defer/async）: %q", tag)
	}
}
