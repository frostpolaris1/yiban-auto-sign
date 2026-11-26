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
	if strings.Contains(csp, "challenges.cloudflare.com") {
		t.Fatalf("未启用人机验证时 CSP 不得放行 Cloudflare（当前: %q）", csp)
	}
}

// 启用人机验证只放开 Cloudflare 控件需要的三条指令；
// script-src 仍不得含 'unsafe-inline'，也不得变成通配符。
func TestSecurityHeadersAllowTurnstileOriginsOnly(t *testing.T) {
	gin.SetMode(gin.TestMode)
	engine, err := New(Options{Sessions: cspWiringIssuer{}, Turnstile: true})
	if err != nil {
		t.Fatalf("router.New: %v", err)
	}

	rec := httptest.NewRecorder()
	engine.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	csp := rec.Header().Get("Content-Security-Policy")
	for _, want := range []string{
		"script-src 'self' https://challenges.cloudflare.com",
		"frame-src https://challenges.cloudflare.com",
		"connect-src 'self' https://challenges.cloudflare.com",
	} {
		if !strings.Contains(csp, want) {
			t.Fatalf("CSP 缺指令 %q（当前: %q）", want, csp)
		}
	}
	if strings.Contains(csp, "unsafe-inline") && strings.Contains(csp, "script-src 'self' 'unsafe-inline'") {
		t.Fatalf("script-src 不得含 'unsafe-inline'（当前: %q）", csp)
	}
	if strings.Contains(csp, "script-src https:") || strings.Contains(csp, "script-src *") {
		t.Fatalf("script-src 不得放宽成通配符（当前: %q）", csp)
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

// must_change_password 门的放行判断必须按 Gin 注册形状精确匹配：
// 方法不符、以允许路径为前缀的路径都不得漏过。
// 该门是强制改密的唯一硬门；用前缀匹配会让 /api/auth/me/xxx 之类漏进去。
func TestMustChangePasswordAllowedMatchesExactRouteAndMethod(t *testing.T) {
	gin.SetMode(gin.TestMode)
	engine := gin.New()
	decisions := make(map[string]bool)
	probe := func(name string) gin.HandlerFunc {
		return func(c *gin.Context) {
			decisions[name] = mustChangePasswordAllowed(c)
		}
	}
	engine.GET("/api/auth/me", probe("get me"))
	engine.POST("/api/auth/me", probe("post me"))
	engine.GET("/api/auth/me/extra", probe("me extra"))
	engine.POST("/api/auth/change-password", probe("post change-password"))
	engine.GET("/api/auth/change-password", probe("get change-password"))
	engine.POST("/api/auth/logout", probe("logout"))
	engine.POST("/api/me/restore", probe("restore"))
	engine.GET("/api/accounts", probe("accounts"))
	engine.POST("/api/me/delete", probe("me delete"))
	engine.GET("/internal/probe", probe("non-public"))

	cases := []struct {
		name   string
		method string
		path   string
		want   bool
	}{
		{"get me", http.MethodGet, "/api/auth/me", true},
		{"post me", http.MethodPost, "/api/auth/me", false},
		{"me extra", http.MethodGet, "/api/auth/me/extra", false},
		{"post change-password", http.MethodPost, "/api/auth/change-password", true},
		{"get change-password", http.MethodGet, "/api/auth/change-password", false},
		{"logout", http.MethodPost, "/api/auth/logout", true},
		{"restore", http.MethodPost, "/api/me/restore", true},
		{"accounts", http.MethodGet, "/api/accounts", false},
		{"me delete", http.MethodPost, "/api/me/delete", false},
		{"non-public", http.MethodGet, "/internal/probe", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			engine.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(tc.method, tc.path, nil))
			got, ok := decisions[tc.name]
			if !ok {
				t.Fatalf("探针未命中：%s %s 未注册", tc.method, tc.path)
			}
			if got != tc.want {
				t.Fatalf("%s %s allowed = %v, want %v", tc.method, tc.path, got, tc.want)
			}
		})
	}
}
