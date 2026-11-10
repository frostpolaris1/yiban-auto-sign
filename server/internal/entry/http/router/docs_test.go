package router_test

// 更新日志读面的门。
//
// # 功能
// 钉住两条用户可见不变式：无会话可读；读不到时回固定回落文案。
//
// # 归属
// entry/http/router 是这条路由的所有者，门与实现同包。
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
