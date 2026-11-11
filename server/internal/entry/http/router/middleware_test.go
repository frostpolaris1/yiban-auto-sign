package router

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"

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

// 安全头走真实中间件链：CSP 必须在每个响应上出现，且 script-src 不含
// 'unsafe-inline'。UI 的内联主题脚本已外置，这条断言与 index.html 结构守卫配套。
func TestSecurityHeadersSetBaselineCSP(t *testing.T) {
	gin.SetMode(gin.TestMode)
	engine := gin.New()
	engine.Use(securityHeaders())
	engine.GET("/", func(c *gin.Context) { c.Status(http.StatusNoContent) })

	rec := httptest.NewRecorder()
	engine.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/", nil))

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
