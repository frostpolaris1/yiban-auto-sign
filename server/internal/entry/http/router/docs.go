package router

// 合规文档与更新日志的只读面。
//
// # 形态
// GET /api/changelog 回 CHANGELOG.md 全文；GET /terms 与 GET /privacy 把
// USER_AGREEMENT.md / PRIVACY_POLICY.md 渲染成独立 HTML 页；
// GET /robots.txt 回爬虫协议。四者都公开，不需要会话。
//
// # 口径
//   - 文档根来自 YIBAN_DOCS_DIR。未设置或文件读不到一律回退内置文案，
//     **不报错**：文档是可选内容，缺了不该挡住服务。
//   - 文件名是包内常量，没有用户输入参与拼路径，故没有穿越面。
//   - 更新日志进程内首读缓存（无 TTL）：部署者换文件后重启生效，对齐旧线语义。
//     协议页每次请求读盘，改文档即时可见。
//   - Markdown 不开原始 HTML：注释与内联标签都不出现在输出里。
//   - 链接只放行 http/https/mailto 与页内 #片段；其余协议（javascript:、
//     data:、file: 与任何自定义协议）降级为纯文本，不输出 href。
//
// # 归属
// entry/http 入口层。本文件只读文件，不写业务数据、不记审计。
//
// # 复用
// newDocsHandler 由 router.New 装配；Markdown 渲染只在本文件出现。
// 渲染器用 github.com/yuin/goldmark（MIT），不自研：旧线自研子集解析器有
// 死循环史，规则 12 也要求通用组件先取社区方案。

import (
	"fmt"
	"html"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"

	"github.com/gin-gonic/gin"
	"github.com/yuin/goldmark"
	"github.com/yuin/goldmark/ast"
	"github.com/yuin/goldmark/parser"
	"github.com/yuin/goldmark/text"
	"github.com/yuin/goldmark/util"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
)

// 文档文件名是常量：路径只由「目录 + 本常量」拼成，无用户输入。
const (
	docsChangelogName = "CHANGELOG.md"
	docsTermsName     = "USER_AGREEMENT.md"
	docsPrivacyName   = "PRIVACY_POLICY.md"
)

// 回落文案。更新日志对齐旧线 api_changelog；协议页对齐旧线 _read_doc_html。
const (
	changelogFallback  = "暂无更新日志"
	docMissingFallback = "文档暂缺，请联系运营者"
)

// robotsTxt 是爬虫协议正文。
//
// 口径移植旧线 web/routes/pages.py 的 robots_txt：只放行登录页与静态资源，
// 其余一律 Disallow。两处有意偏差：
//  1. 旧线按 request.script_root 插值挂载前缀（子路径部署）；Go 线单进程根挂载，
//     没有子路径形态，插值只会写出爬虫抓不到的地址，故去掉。
//  2. 旧线的静态前缀是 /static/；Go 线的前端产物在 /assets/ 下（见 ui.go），
//     照抄 /static/ 会是一条永远不命中的放行。
const robotsTxt = `User-agent: *
Allow: /login
Allow: /assets/
Disallow: /
`

// docPageShell 是协议页的最小外壳：标题、charset 与返回登录页的链接。
// 响应头由 securityHeaders 中间件统一给，本文件不重复设置。
const docPageShell = `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%s · 易班自动签到</title>
</head>
<body>
<main>
%s
<p><a href="/login">← 返回登录页</a></p>
</main>
</body>
</html>
`

// docMarkdown 是协议页的渲染器。
// 原始 HTML 保持关闭（缺省即 Unsafe=false）：HTML 块与内联标签都渲染成
// 「raw HTML omitted」占位，注释里的内容不进输出。
var docMarkdown = goldmark.New(
	goldmark.WithParserOptions(parser.WithASTTransformers(
		util.Prioritized(docLinkWhitelist{}, 100),
	)),
)

// docsHandler 服务一个只读文档目录。
type docsHandler struct {
	dir string
	// once 与 changelog 一起构成首读缓存：读不到也缓存回落文案，
	// 与旧线一致（部署重启自然失效）。
	once      sync.Once
	changelog string
}

// newDocsHandler 构造处理器。dir 为空时全部回退文案。
func newDocsHandler(dir string) *docsHandler {
	return &docsHandler{dir: dir}
}

// changelogText 返回更新日志全文，首读后缓存。
func (d *docsHandler) changelogText() string {
	d.once.Do(func() {
		d.changelog = changelogFallback
		if d.dir == "" {
			return
		}
		body, err := os.ReadFile(filepath.Join(d.dir, docsChangelogName))
		if err != nil {
			return
		}
		d.changelog = string(body)
	})
	return d.changelog
}

// ChangelogGet 是 GET /api/changelog 的契约处理器。
func (s *server) ChangelogGet(c *gin.Context) {
	respond.JSON(c.Writer, http.StatusOK, oapi.Changelog{Text: s.docs.changelogText()})
}

// terms 是 GET /terms：用户协议独立页。
func (d *docsHandler) terms(c *gin.Context) {
	d.serveDoc(c, "用户协议", docsTermsName)
}

// privacy 是 GET /privacy：隐私政策独立页。
func (d *docsHandler) privacy(c *gin.Context) {
	d.serveDoc(c, "隐私政策", docsPrivacyName)
}

// serveDoc 读文档、渲染并包成独立页。
// title 只由本文件的字面量传入，仍按属性上下文转义一次：
// 将来若改成可配置来源，漏转义就是直接落进 <title>。
func (d *docsHandler) serveDoc(c *gin.Context, title, name string) {
	body := docMissingFallback
	if d.dir != "" {
		if raw, err := os.ReadFile(filepath.Join(d.dir, name)); err == nil {
			body = renderDocMarkdown(raw)
		}
	}
	page := fmt.Sprintf(docPageShell, html.EscapeString(title), body)
	c.Data(http.StatusOK, "text/html; charset=utf-8", []byte(page))
}

// robots 是 GET /robots.txt。
// 短缓存对齐旧线：换协议后一小时生效，不必等爬虫自己重抓。
func (d *docsHandler) robots(c *gin.Context) {
	c.Header("Cache-Control", "public, max-age=3600")
	c.Data(http.StatusOK, "text/plain; charset=utf-8", []byte(robotsTxt))
}

// renderDocMarkdown 把一份 Markdown 渲染成 HTML 片段。
// 渲染失败或正文为空时回退占位文案：空模板只剩注释，回显编辑者注释没有意义。
func renderDocMarkdown(source []byte) string {
	var out strings.Builder
	if err := docMarkdown.Convert(source, &out); err != nil {
		return docMissingFallback
	}
	if strings.TrimSpace(out.String()) == "" {
		return docMissingFallback
	}
	return out.String()
}

// docLinkWhitelist 把白名单外的链接、图片与自动链接降级为纯文本。
//
// 为什么不用 goldmark 自带的 IsDangerousURL：它只挡 javascript:/vbscript:/
// file:/data: 四种，ftp:、tel: 与任何自定义协议仍会落成可点的 href。
// 本项目的口径是**白名单**（http/https/mailto 与页内 #片段），不是黑名单。
type docLinkWhitelist struct{}

// Transform 实现 parser.ASTTransformer。
// 先收集再改写：边遍历边摘节点会让 Walk 的兄弟指针失效。
func (docLinkWhitelist) Transform(doc *ast.Document, reader text.Reader, _ parser.Context) {
	source := reader.Source()
	var targets []ast.Node
	_ = ast.Walk(doc, func(n ast.Node, entering bool) (ast.WalkStatus, error) {
		if !entering {
			return ast.WalkContinue, nil
		}
		switch n.(type) {
		case *ast.Link, *ast.Image, *ast.AutoLink:
			targets = append(targets, n)
		}
		return ast.WalkContinue, nil
	})
	for _, n := range targets {
		switch node := n.(type) {
		case *ast.Link:
			if !docLinkAllowed(node.Destination) {
				docReplaceWithChildren(node)
			}
		case *ast.Image:
			// 图片同样按链接口径判：白名单外的 src 不落成 <img>，
			// 只保留 alt 文本（子节点）。
			if !docLinkAllowed(node.Destination) {
				docReplaceWithChildren(node)
			}
		case *ast.AutoLink:
			if docAutoLinkAllowed(node) {
				continue
			}
			if parent := node.Parent(); parent != nil {
				parent.ReplaceChild(parent, node, ast.NewString(node.Label(source)))
			}
		}
	}
}

// docAutoLinkAllowed 判 <scheme:...> 自动链接。
// Protocol 为 nil 是邮箱自动链接，渲染成 mailto:，放行。
func docAutoLinkAllowed(node *ast.AutoLink) bool {
	if node.Protocol == nil {
		return true
	}
	switch strings.ToLower(string(node.Protocol)) {
	case "http", "https", "mailto":
		return true
	}
	return false
}

// docLinkAllowed 是链接协议白名单。
// 只放行 http/https/mailto 与页内 #片段；大小写不敏感，前后空白先去掉。
func docLinkAllowed(dest []byte) bool {
	target := strings.ToLower(strings.TrimSpace(string(dest)))
	if strings.HasPrefix(target, "#") {
		return true
	}
	for _, scheme := range []string{"http://", "https://", "mailto:"} {
		if strings.HasPrefix(target, scheme) {
			return true
		}
	}
	return false
}

// docReplaceWithChildren 用节点的子节点替换节点本身。
// 链接的标签文本（或图片的 alt 文本）原样留在正文里，href/src 不再输出。
func docReplaceWithChildren(node ast.Node) {
	parent := node.Parent()
	if parent == nil {
		return
	}
	for child := node.FirstChild(); child != nil; {
		next := child.NextSibling()
		parent.InsertBefore(parent, node, child)
		child = next
	}
	parent.RemoveChild(parent, node)
}
