package router

// 前端构建产物的静态托管。
//
// # 形态
// 一个进程同时提供页面与 /api。生产只需一层反向代理终结 TLS，
// 不需要为静态资源再信任一层代理的 X-Forwarded-*。
//
// # 口径
//   - YIBAN_UI_DIR 为空时只提供 API：未知路径仍是 JSON 404。
//   - 设置了目录但缺 index.html 时启动失败：宁可起不来，也不带病上线。
//   - /api 与 /api/* 的未知路径永远是 JSON 404，不落页面。
//   - 无扩展名的未知路径回 index.html（前端深链）；带扩展名的不回。
//   - assets/ 下的哈希资产长缓存；index.html 每次重验证；
//     根级 png（站标）一小时短缓存。
//   - 读面用 os.Root：符号链接也不能逃出根目录。

import (
	"fmt"
	"io/fs"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"strings"

	"github.com/gin-gonic/gin"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
)

const uiIndexName = "index.html"

// uiHandler 服务一个已构建的前端目录。
// 它只读文件，不写、不列目录。
type uiHandler struct {
	dir string
}

// newUIHandler 校验目录并构造处理器。
// 启动期失败胜过运行期每个请求都 404。
func newUIHandler(dir string) (*uiHandler, error) {
	abs, err := filepath.Abs(dir)
	if err != nil {
		return nil, fmt.Errorf("router: YIBAN_UI_DIR=%q: %w", dir, err)
	}
	root, err := os.OpenRoot(abs)
	if err != nil {
		return nil, fmt.Errorf("router: YIBAN_UI_DIR=%q: %w", dir, err)
	}
	info, err := root.Stat(uiIndexName)
	_ = root.Close()
	if err != nil {
		return nil, fmt.Errorf("router: YIBAN_UI_DIR=%q has no %s: %w", dir, uiIndexName, err)
	}
	if !info.Mode().IsRegular() {
		return nil, fmt.Errorf("router: YIBAN_UI_DIR=%q: %s is not a regular file", dir, uiIndexName)
	}
	return &uiHandler{dir: abs}, nil
}

// serve 是 NoRoute 处理器：静态文件优先，其次前端深链，最后 JSON 404。
func (u *uiHandler) serve(c *gin.Context) {
	if isAPIPath(c.Request.URL.Path) {
		routeNotFound(c)
		return
	}
	if c.Request.Method != http.MethodGet && c.Request.Method != http.MethodHead {
		routeNotFound(c)
		return
	}

	// 每请求开一次 Root：读面钉在目录内，且不把目录句柄留到进程退出
	//（Windows 上句柄会让测试的临时目录无法清理）。
	root, err := os.OpenRoot(u.dir)
	if err != nil {
		routeNotFound(c)
		return
	}
	defer func() { _ = root.Close() }()
	fsys := root.FS()

	rel, hasRel := uiRelativePath(c.Request.URL.Path)
	if hasRel && isRegularFile(fsys, rel) {
		u.setCacheHeader(c.Writer.Header(), rel)
		http.ServeFileFS(c.Writer, c.Request, fsys, rel)
		return
	}
	// 带扩展名的缺失文件不回 index：否则缺资产会拿到 HTML 与 200。
	if hasRel && looksLikeFile(rel) {
		routeNotFound(c)
		return
	}
	// 运行期 index.html 被删或换成目录时不列出目录项。
	if !isRegularFile(fsys, uiIndexName) {
		routeNotFound(c)
		return
	}
	u.setCacheHeader(c.Writer.Header(), uiIndexName)
	http.ServeFileFS(c.Writer, c.Request, fsys, uiIndexName)
}

func isRegularFile(fsys fs.FS, name string) bool {
	info, err := fs.Stat(fsys, name)
	return err == nil && info.Mode().IsRegular()
}

// setCacheHeader 只给哈希资产长缓存。
// index.html 必须每次重验证：它指向当次构建的资产名。
// 根级 png（部署者自放的站标）取一小时短缓存：换图后不必等长缓存过期。
func (u *uiHandler) setCacheHeader(h http.Header, name string) {
	if strings.HasPrefix(name, "assets/") {
		h.Set("Cache-Control", "public, max-age=31536000, immutable")
		return
	}
	if !strings.Contains(name, "/") && strings.HasSuffix(strings.ToLower(name), ".png") {
		h.Set("Cache-Control", "public, max-age=3600")
		return
	}
	h.Set("Cache-Control", "no-cache")
}

// isAPIPath 判 API 前缀。它先于静态查找：API 的未知路径不得变成页面。
func isAPIPath(p string) bool {
	return p == "/api" || strings.HasPrefix(p, "/api/")
}

// uiRelativePath 把 URL 路径收敛成目录内的相对路径。
// path.Clean 消掉 ..；符号链接由 os.Root 挡住。
func uiRelativePath(urlPath string) (string, bool) {
	clean := path.Clean("/" + urlPath)
	rel := strings.TrimPrefix(clean, "/")
	if rel == "" || rel == "." {
		return "", false
	}
	return rel, true
}

// looksLikeFile 判最后一段是否带扩展名。
func looksLikeFile(name string) bool {
	return strings.Contains(path.Base(name), ".")
}

// routeNotFound 是未设置 UI 或明确缺失时的统一 404。
func routeNotFound(c *gin.Context) {
	respond.Error(c.Writer, c.Request, errs.New(errs.CodeNotFound, "route not found"))
}
