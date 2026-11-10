package router_test

// 文档只读面与静态页路由的门。
//
// # 功能
// 钉住四条用户可见不变式：
//   - /api/changelog 无会话可读，读不到时回固定回落文案；
//   - /terms 与 /privacy 的 Markdown 注释不进正文，白名单外链接不落 href；
//   - /robots.txt 的禁抓集合；
//   - 站标缺失 404、存在一小时短缓存。
//
// # 归属
// entry/http/router 是这些路由的所有者，门与实现同包。
// 本文件**不**连数据库、**不**读真实仓库文档：fixture 全部在临时目录里造。
//
// # 口径
// 断言用户可见输出，不钉日志串与内部调用顺序。

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/router"
)

// writeDocsDir 造一个最小文档目录。
func writeDocsDir(t *testing.T, files map[string]string) string {
	t.Helper()
	dir := t.TempDir()
	for name, body := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
			t.Fatalf("写 %s: %v", name, err)
		}
	}
	return dir
}

// newDocsEngine 造一个只挂文档面的引擎。
func newDocsEngine(t *testing.T, docsDir string) http.Handler {
	t.Helper()
	engine, err := router.New(router.Options{Sessions: newStubIssuer(), DocsDir: docsDir})
	if err != nil {
		t.Fatalf("router.New: %v", err)
	}
	return engine
}

// changelogText 读 /api/changelog 的 text 字段。
func changelogText(t *testing.T, h http.Handler) string {
	t.Helper()
	rec := get(t, h, "/api/changelog")
	if rec.Code != http.StatusOK {
		t.Fatalf("GET /api/changelog = %d, want 200", rec.Code)
	}
	var body struct {
		Text *string `json:"text"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("解析响应失败: %v（%q）", err, rec.Body.String())
	}
	if body.Text == nil {
		t.Fatalf("响应缺 text 字段: %q", rec.Body.String())
	}
	return *body.Text
}

// 公开读面：无会话也 200，正文是文档全文。
func TestChangelogIsPublicAndServesDocument(t *testing.T) {
	const marker = "changelog-marker-2026-11-10"
	h := newDocsEngine(t, writeDocsDir(t, map[string]string{
		"CHANGELOG.md": "# 更新日志\n\n" + marker + "\n",
	}))
	if got := changelogText(t, h); !strings.Contains(got, marker) {
		t.Fatalf("changelog 未返回文档正文: %q", got)
	}
}

// 未设置文档目录：仍是 200，正文是固定回落文案。
func TestChangelogFallsBackWithoutDocsDir(t *testing.T) {
	h := newDocsEngine(t, "")
	if got := changelogText(t, h); got != "暂无更新日志" {
		t.Fatalf("changelog 回落 = %q, want 暂无更新日志", got)
	}
}

// 目录在、文件缺失：同样是回落文案，不是错误。
func TestChangelogFallsBackWhenFileMissing(t *testing.T) {
	h := newDocsEngine(t, t.TempDir())
	if got := changelogText(t, h); got != "暂无更新日志" {
		t.Fatalf("changelog 回落 = %q, want 暂无更新日志", got)
	}
}

// 协议页：注释里的内容不得出现在正文。
// 旧线自研渲染器专门跳过 <!-- -->；换成 goldmark 后靠「原始 HTML 关闭」守同一条线。
func TestDocPagesDoNotLeakHTMLComments(t *testing.T) {
	const secret = "comment-secret-2026-11-10"
	h := newDocsEngine(t, writeDocsDir(t, map[string]string{
		"USER_AGREEMENT.md": "<!-- " + secret + " -->\n\n# 用户协议\n\n正文段落。\n",
		"PRIVACY_POLICY.md": "首段 <!-- " + secret + " --> 尾段。\n\n# 隐私政策\n",
	}))
	for _, target := range []string{"/terms", "/privacy"} {
		rec := get(t, h, target)
		if rec.Code != http.StatusOK {
			t.Fatalf("GET %s = %d, want 200", target, rec.Code)
		}
		if body := rec.Body.String(); strings.Contains(body, secret) {
			t.Fatalf("GET %s 泄漏了注释内容: %q", target, body)
		}
	}
}

// 负向用例：白名单外协议不落 href，标签文本保留。
func TestDocPagesDropUnsafeLinkSchemes(t *testing.T) {
	h := newDocsEngine(t, writeDocsDir(t, map[string]string{
		"USER_AGREEMENT.md": strings.Join([]string{
			"# 用户协议",
			"",
			"[点我](javascript:alert(1))",
			"",
			"[数据](data:text/html;base64,PHNjcmlwdD4=)",
			"",
			"[自定](tel:10086)",
			"",
			"[放行](https://example.com/doc)",
			"",
			"[片段](#section)",
		}, "\n"),
	}))
	rec := get(t, h, "/terms")
	if rec.Code != http.StatusOK {
		t.Fatalf("GET /terms = %d, want 200", rec.Code)
	}
	body := rec.Body.String()
	for _, banned := range []string{"javascript:", "data:text/html", "tel:10086"} {
		if strings.Contains(body, banned) {
			t.Fatalf("正文含未放行的协议 %q: %q", banned, body)
		}
	}
	for _, label := range []string{"点我", "数据", "自定"} {
		if !strings.Contains(body, label) {
			t.Fatalf("降级后丢了链接文本 %q: %q", label, body)
		}
	}
	if !strings.Contains(body, `href="https://example.com/doc"`) {
		t.Fatalf("https 链接未落 href: %q", body)
	}
	if !strings.Contains(body, `href="#section"`) {
		t.Fatalf("页内片段未落 href: %q", body)
	}
}

// 自动链接（<...>）单独一组：goldmark v1.8.7 不填 AutoLink.Protocol，
// 按 Protocol 判会得到「nil 即放行」的死分支，<tel:10086> 会落成可点 href。
// 降级后文本保留（它就是标签），但 href 集合里不得出现白名单外的协议。
func TestDocPagesDropUnsafeAutoLinks(t *testing.T) {
	h := newDocsEngine(t, writeDocsDir(t, map[string]string{
		"USER_AGREEMENT.md": strings.Join([]string{
			"# 用户协议",
			"",
			"<tel:10086>",
			"",
			"<javascript:alert(1)>",
			"",
			"<https://example.com/a>",
			"",
			"<a@example.com>",
		}, "\n"),
	}))
	rec := get(t, h, "/terms")
	if rec.Code != http.StatusOK {
		t.Fatalf("GET /terms = %d, want 200", rec.Code)
	}
	body := rec.Body.String()
	for _, href := range docHrefs(body) {
		switch {
		case strings.HasPrefix(href, "https://example.com/a"),
			strings.HasPrefix(href, "mailto:a@example.com"),
			strings.HasPrefix(href, "/login"):
		default:
			t.Fatalf("出现白名单外的 href %q: %q", href, body)
		}
	}
	// 降级为纯文本：文本还在，标签不在。
	if !strings.Contains(body, "tel:10086") || strings.Contains(body, `href="tel:`) {
		t.Fatalf("tel 自动链接未降级为纯文本: %q", body)
	}
	if !strings.Contains(body, "javascript:alert(1)") || strings.Contains(body, `href="javascript:`) {
		t.Fatalf("javascript 自动链接未降级为纯文本: %q", body)
	}
	if !strings.Contains(body, `href="https://example.com/a"`) {
		t.Fatalf("https 自动链接未落 href: %q", body)
	}
	if !strings.Contains(body, `href="mailto:a@example.com"`) {
		t.Fatalf("邮箱自动链接未落 mailto: %q", body)
	}
}

// docHrefs 取出正文里全部 href 值。
// 断言按集合判，不按出现顺序：渲染器的标签顺序不是本门的判据。
func docHrefs(body string) []string {
	var out []string
	for _, part := range strings.Split(body, `href="`)[1:] {
		if end := strings.IndexByte(part, '"'); end >= 0 {
			out = append(out, part[:end])
		}
	}
	return out
}

// 文档缺失：协议页仍 200，正文是一句占位文案。
func TestDocPagesFallBackWhenFileMissing(t *testing.T) {
	h := newDocsEngine(t, t.TempDir())
	for _, target := range []string{"/terms", "/privacy"} {
		rec := get(t, h, target)
		if rec.Code != http.StatusOK {
			t.Fatalf("GET %s = %d, want 200", target, rec.Code)
		}
		if !strings.Contains(rec.Body.String(), "文档暂缺，请联系运营者") {
			t.Fatalf("GET %s 缺占位文案: %q", target, rec.Body.String())
		}
	}
}

// 未设置文档目录：与文件缺失同一条回落路径。
func TestDocPagesFallBackWithoutDocsDir(t *testing.T) {
	h := newDocsEngine(t, "")
	for _, target := range []string{"/terms", "/privacy"} {
		rec := get(t, h, target)
		if rec.Code != http.StatusOK {
			t.Fatalf("GET %s = %d, want 200", target, rec.Code)
		}
		if !strings.Contains(rec.Body.String(), "文档暂缺，请联系运营者") {
			t.Fatalf("GET %s 缺占位文案: %q", target, rec.Body.String())
		}
	}
}

// 只剩注释的模板：占位注释不得被当成正文，仍走「文档暂缺」。
func TestDocPagesFallBackWhenOnlyComments(t *testing.T) {
	h := newDocsEngine(t, writeDocsDir(t, map[string]string{
		"USER_AGREEMENT.md": "<!-- 编辑者说明：正文待补 -->\n",
	}))
	rec := get(t, h, "/terms")
	if rec.Code != http.StatusOK {
		t.Fatalf("GET /terms = %d, want 200", rec.Code)
	}
	body := rec.Body.String()
	if !strings.Contains(body, "文档暂缺，请联系运营者") {
		t.Fatalf("全注释模板未回落: %q", body)
	}
	if strings.Contains(body, "raw HTML omitted") {
		t.Fatalf("占位注释被当成正文输出: %q", body)
	}
}

// 各路由的 Content-Type：JSON 读面与 HTML/文本页各按自己的类型回。
func TestDocsRoutesContentType(t *testing.T) {
	h := newDocsEngine(t, writeDocsDir(t, map[string]string{
		"CHANGELOG.md":      "# 更新日志\n",
		"USER_AGREEMENT.md": "# 用户协议\n",
		"PRIVACY_POLICY.md": "# 隐私政策\n",
	}))
	cases := []struct {
		target string
		want   string
	}{
		{"/api/changelog", "application/json; charset=utf-8"},
		{"/terms", "text/html; charset=utf-8"},
		{"/privacy", "text/html; charset=utf-8"},
	}
	for _, tc := range cases {
		rec := get(t, h, tc.target)
		if rec.Code != http.StatusOK {
			t.Fatalf("GET %s = %d, want 200", tc.target, rec.Code)
		}
		if got := rec.Header().Get("Content-Type"); got != tc.want {
			t.Fatalf("GET %s Content-Type = %q, want %q", tc.target, got, tc.want)
		}
	}
}

// 更新日志是首读缓存：第一次读不到就钉住回落值，之后补文件也不变。
// 部署者换文档要重启，这条钉的就是 sync.Once 的口径。
func TestChangelogFallbackIsCached(t *testing.T) {
	dir := t.TempDir()
	h := newDocsEngine(t, dir)
	if got := changelogText(t, h); got != "暂无更新日志" {
		t.Fatalf("首次读不到时 = %q, want 暂无更新日志", got)
	}
	if err := os.WriteFile(filepath.Join(dir, "CHANGELOG.md"),
		[]byte("# 更新日志\n\n后补内容\n"), 0o644); err != nil {
		t.Fatalf("补写 CHANGELOG.md: %v", err)
	}
	if got := changelogText(t, h); got != "暂无更新日志" {
		t.Fatalf("首读缓存失效：后补文件后 = %q, want 暂无更新日志", got)
	}
}

// 爬虫协议：只放行登录页与静态资源，其余全禁。
// 对齐旧线 tests/test_static_routes.py 的 test_robots_disallows_private_pages。
func TestRobotsTxtDisallowsPrivatePages(t *testing.T) {
	h := newDocsEngine(t, "")
	rec := get(t, h, "/robots.txt")
	if rec.Code != http.StatusOK {
		t.Fatalf("GET /robots.txt = %d, want 200", rec.Code)
	}
	if got := rec.Header().Get("Content-Type"); !strings.HasPrefix(got, "text/plain") {
		t.Fatalf("Content-Type = %q, want text/plain", got)
	}
	want := "User-agent: *\nAllow: /login\nAllow: /assets/\nDisallow: /\n"
	if got := rec.Body.String(); got != want {
		t.Fatalf("robots.txt = %q, want %q", got, want)
	}
}

// 站标：缺失 404（浏览器退回默认图标），存在一小时短缓存。
func TestFaviconMissingAndShortCache(t *testing.T) {
	dir := writeUIDist(t)
	h := newUIEngine(t, dir)

	rec := get(t, h, "/favicon.png")
	if rec.Code != http.StatusNotFound {
		t.Fatalf("缺 favicon 时 GET /favicon.png = %d, want 404", rec.Code)
	}

	if err := os.WriteFile(filepath.Join(dir, "favicon.png"), []byte("png-bytes"), 0o644); err != nil {
		t.Fatalf("写 favicon: %v", err)
	}
	rec = get(t, h, "/favicon.png")
	if rec.Code != http.StatusOK {
		t.Fatalf("有 favicon 时 GET /favicon.png = %d, want 200", rec.Code)
	}
	if got := rec.Header().Get("Cache-Control"); got != "public, max-age=3600" {
		t.Fatalf("favicon Cache-Control = %q, want public, max-age=3600", got)
	}
}
