package router

// 更新日志读面。
//
// # 形态
// GET /api/changelog 回 CHANGELOG.md 全文。公开，不需要会话。
//
// # 口径
//   - 文档根来自 YIBAN_DOCS_DIR。未设置或文件读不到一律回退内置文案，
//     **不报错**：文档是可选内容，缺了不该挡住服务。
//   - 文件名是包内常量，没有用户输入参与拼路径，故没有穿越面。
//   - 进程内首读缓存（无 TTL）：部署者换文件后重启生效，对齐旧线语义。
//
// # 归属
// entry/http 入口层。本文件只读文件，不写业务数据、不记审计。
//
// # 复用
// newDocsHandler 由 router.New 装配。

import (
	"net/http"
	"os"
	"path/filepath"
	"sync"

	"github.com/gin-gonic/gin"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
)

// docsChangelogName 是更新日志文件名。
// 路径只由「目录 + 本常量」拼成，无用户输入。
const docsChangelogName = "CHANGELOG.md"

// changelogFallback 是读不到文档时的正文，对齐旧线 api_changelog。
const changelogFallback = "暂无更新日志"

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
