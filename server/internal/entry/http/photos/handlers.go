// Package photos 把图片库用例映射到 HTTP。
//
// # 功能
// 自有图片五端点（列表、上传、读取、软删、保留期）、
// 按账号列照片、学校默认图读写。
// 上传走 multipart/form-data，单文件字段名 image。
// 读取直接流式写响应，先判作用域再写字节。
// 本包**不**解码图片、**不**写库、**不**决定保留期。
//
// # 归属
// entry/http 入口层。作用域由 Authorizer（app/admin）解析；
// 用例由 app/photos 提供，blob 由 adapters/storage 提供。
//
// # 复用
// Handler、NewHandler；Service、Authorizer。
//
// # 口径
// 上传原始字节上限 10 MiB；路由层的请求体上限是 11 MiB。
// 响应不含手机号与凭据；图片字节只经 GET 单图端点返回。
// 越权与不存在统一 404；动作无角色回 403。
package photos

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appphotos "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/photos"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corephoto "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/photo"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
)

// 路由级 deadline 预算。
//
// 全局 http.Server 的 ReadTimeout/WriteTimeout 从**读到请求头**起算：
// 10 MiB 上传与慢链路下载都会越过它，连接在 handler 中途被切断。
// 本文件只对照片路由放宽本次连接的截止时间，不放宽全局值。
// 机制与 SSE 清写超时相同（events/sse.go）：Gin 的 ResponseWriter
// 实现 Unwrap，ResponseController 因此能拿到底层连接。
const (
	photoUploadReadBudget  = 2 * time.Minute
	photoUploadWriteBudget = 2 * time.Minute
	photoGetWriteBudget    = 5 * time.Minute
)

// Service 是图片库用例端口（实现为 app/photos.Service）。
type Service interface {
	Upload(ctx context.Context, scope corescope.Scope, actor appaudit.Actor, raw []byte) (corephoto.Record, error)
	List(ctx context.Context, scope corescope.Scope, cursor string, limit int) (appphotos.Page, error)
	ListAccount(ctx context.Context, scope corescope.Scope, accountID, cursor string, limit int) (appphotos.Page, error)
	Get(ctx context.Context, scope corescope.Scope, id string) (corephoto.Record, error)
	Open(ctx context.Context, scope corescope.Scope, id string) (corephoto.Record, io.ReadCloser, error)
	Delete(ctx context.Context, scope corescope.Scope, id string) (corephoto.Record, error)
	SetRetention(ctx context.Context, scope corescope.Scope, days int) (int, error)
	GetSchoolDefault(ctx context.Context, scope corescope.Scope, schoolID string) (appphotos.SchoolDefaultPhoto, error)
	SetSchoolDefault(ctx context.Context, scope corescope.Scope, schoolID string, photoID *string, actor appaudit.Actor) (appphotos.SchoolDefaultPhoto, error)
}

// Authorizer 把会话主体解析成作用域与审计主体。
type Authorizer interface {
	Authorize(ctx context.Context, user coreauth.User) (corescope.Scope, appaudit.Actor, error)
}

// Handler 处理 /api/me/photos 下的端点。
type Handler struct {
	svc    Service
	authz  Authorizer
	logger *slog.Logger
	// clock 只服务于路由级 deadline：截止时间必须取真实时间。
	clock coreclock.Clock
}

// NewHandler 构造处理器。日志缺省用进程 default logger。
func NewHandler(svc Service, authz Authorizer) *Handler {
	return &Handler{
		svc: svc, authz: authz,
		logger: slog.Default(), clock: coreclock.System{},
	}
}

// WithLogger 覆盖处理器日志。装配层传进程 logger。
func (h *Handler) WithLogger(logger *slog.Logger) *Handler {
	if logger != nil {
		h.logger = logger
	}
	return h
}

// PhotoList 实现 GET /api/me/photos。
func (h *Handler) PhotoList(c *gin.Context, params oapi.PhotoListParams) {
	scope, _, ok := h.principal(c)
	if !ok {
		return
	}
	// limit 与其余列表同口径：缺省取 50，越界回 400。
	limit, err := app.PageLimit(params.Limit)
	if err != nil {
		fail(c, err)
		return
	}
	cursor := ""
	if params.Cursor != nil {
		cursor = string(*params.Cursor)
	}
	page, err := h.svc.List(c.Request.Context(), scope, cursor, limit)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, photoPageView(page))
}

// AccountPhotoList 实现 GET /api/accounts/{id}/photos。
// 可见面由用例判定：跨用户、跨校与已软删账号一律 404。
func (h *Handler) AccountPhotoList(c *gin.Context, id oapi.AccountID, params oapi.AccountPhotoListParams) {
	scope, _, ok := h.principal(c)
	if !ok {
		return
	}
	limit, err := app.PageLimit(params.Limit)
	if err != nil {
		fail(c, err)
		return
	}
	cursor := ""
	if params.Cursor != nil {
		cursor = string(*params.Cursor)
	}
	page, err := h.svc.ListAccount(c.Request.Context(), scope, string(id), cursor, limit)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, photoPageView(page))
}

// SchoolDefaultPhotoGet 实现 GET /api/schools/{id}/default-photo。
func (h *Handler) SchoolDefaultPhotoGet(c *gin.Context, id oapi.SchoolID) {
	scope, _, ok := h.principal(c)
	if !ok {
		return
	}
	out, err := h.svc.GetSchoolDefault(c.Request.Context(), scope, string(id))
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, schoolDefaultView(out))
}

// SchoolDefaultPhotoSet 实现 PUT /api/schools/{id}/default-photo。
// photo_id 键必须出现；显式 null 才是「清除默认图」（契约 required）。
func (h *Handler) SchoolDefaultPhotoSet(c *gin.Context, id oapi.SchoolID, _ oapi.SchoolDefaultPhotoSetParams) {
	scope, actor, ok := h.principal(c)
	if !ok {
		return
	}
	raw, err := io.ReadAll(io.LimitReader(c.Request.Body, 1<<20))
	if err != nil {
		respond.BindError(c.Writer, c.Request, err)
		return
	}
	var keys map[string]json.RawMessage
	if err := json.Unmarshal(raw, &keys); err != nil {
		respond.BindError(c.Writer, c.Request, err)
		return
	}
	if _, present := keys["photo_id"]; !present {
		fail(c, errs.New(errs.CodeValidation, "photo_id is required; send null to clear the default photo"))
		return
	}
	var body oapi.SchoolDefaultPhotoUpdate
	if err := json.Unmarshal(raw, &body); err != nil {
		respond.BindError(c.Writer, c.Request, err)
		return
	}
	out, err := h.svc.SetSchoolDefault(c.Request.Context(), scope, string(id), body.PhotoId, actor)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, schoolDefaultView(out))
}

// PhotoUpload 实现 POST /api/me/photos。
//
// 请求体可到 11 MiB，慢链路读取会超过全局 ReadTimeout；
// 读与写都按本路由的预算放宽。写也放宽：请求体读完时全局
// WriteTimeout 往往已过期，不放宽则 201 写不出去。
func (h *Handler) PhotoUpload(c *gin.Context, _ oapi.PhotoUploadParams) {
	h.extendReadDeadline(c, photoUploadReadBudget)
	h.extendWriteDeadline(c, photoUploadWriteBudget)
	scope, actor, ok := h.principal(c)
	if !ok {
		return
	}
	file, err := uploadFile(c)
	if err != nil {
		// 超过路由上限时是 *http.MaxBytesError；BindError 把它转成 413。
		respond.BindError(c.Writer, c.Request, err)
		return
	}
	raw, err := io.ReadAll(io.LimitReader(file, corephoto.MaxBytes+1))
	file.Close()
	// ParseMultipartForm 把超过内存阈值的 part 写进系统临时目录，
	// Go 只在解析失败时自清。字节已读进内存，这里必须显式删除：
	// 原始字节未去 EXIF，长期落盘等于把隐私面留在磁盘上。
	if c.Request.MultipartForm != nil {
		_ = c.Request.MultipartForm.RemoveAll()
	}
	if err != nil {
		fail(c, errs.New(errs.CodeValidation, "cannot read the image part"))
		return
	}
	if len(raw) > corephoto.MaxBytes {
		fail(c, errs.New(errs.CodeValidation, "image is too large"))
		return
	}
	rec, err := h.svc.Upload(c.Request.Context(), scope, actor, raw)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusCreated, photoView(rec))
}

// PhotoGet 实现 GET /api/me/photos/{id}。
func (h *Handler) PhotoGet(c *gin.Context, id oapi.PhotoID, params oapi.PhotoGetParams) {
	scope, _, ok := h.principal(c)
	if !ok {
		return
	}
	rec, body, err := h.svc.Open(c.Request.Context(), scope, string(id))
	if err != nil {
		fail(c, err)
		return
	}
	defer body.Close()
	etag := `"` + hex.EncodeToString(rec.SHA256[:]) + `"`
	c.Header("ETag", etag)
	c.Header("Cache-Control", "private, max-age=0")
	// 条件请求：If-None-Match 命中则 304（无响应体）。只回 ETag 与缓存头，
	// 不带 Content-Type/Content-Length——304 不许有载荷（7x2-3）。
	inm := ""
	if params.IfNoneMatch != nil {
		inm = *params.IfNoneMatch
	}
	if ifNoneMatchHit(inm, etag) {
		c.Status(http.StatusNotModified)
		return
	}
	// 大图在慢链路上超过全局 WriteTimeout；写头前放宽本次连接的写截止时间。
	h.extendWriteDeadline(c, photoGetWriteBudget)
	// 响应头在写第一个字节前固定：作用域判据已通过。
	c.Header("Content-Type", rec.Mime)
	c.Header("Content-Length", strconv.FormatInt(rec.Bytes, 10))
	c.Status(http.StatusOK)
	// 头部已发出，复制失败无法改状态码；客户端断开属正常终止。
	_, _ = io.Copy(c.Writer, body)
}

// ifNoneMatchHit 按 RFC 9110 的 If-None-Match 语义判命中：
// 逗号列表、弱比较（W/ 前缀两侧等价）、`*` 匹配任一表示。
func ifNoneMatchHit(header, etag string) bool {
	for _, candidate := range strings.Split(header, ",") {
		candidate = strings.TrimSpace(candidate)
		if candidate == "*" {
			return true
		}
		if strings.TrimPrefix(candidate, "W/") == etag {
			return true
		}
	}
	return false
}

// PhotoDelete 实现 DELETE /api/me/photos/{id}。
func (h *Handler) PhotoDelete(c *gin.Context, id oapi.PhotoID, _ oapi.PhotoDeleteParams) {
	scope, _, ok := h.principal(c)
	if !ok {
		return
	}
	if _, err := h.svc.Delete(c.Request.Context(), scope, string(id)); err != nil {
		fail(c, err)
		return
	}
	c.Status(http.StatusNoContent)
}

// PhotoRetentionSet 实现 PUT /api/me/photo-retention。
func (h *Handler) PhotoRetentionSet(c *gin.Context, _ oapi.PhotoRetentionSetParams) {
	var body oapi.PhotoRetentionUpdate
	if err := c.ShouldBindJSON(&body); err != nil {
		respond.BindError(c.Writer, c.Request, err)
		return
	}
	scope, _, ok := h.principal(c)
	if !ok {
		return
	}
	days, err := h.svc.SetRetention(c.Request.Context(), scope, int(body.Days))
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, oapi.PhotoRetention{Days: oapi.PhotoRetentionDays(days)})
}

// uploadFile 取出 multipart 的 image 文件部分。
// 解析失败原样返回：调用方经 respond.BindError 把超界转成 413。
func uploadFile(c *gin.Context) (io.ReadCloser, error) {
	// 1 MiB 留在内存，其余落临时文件；请求体总上限由路由层控制。
	if err := c.Request.ParseMultipartForm(1 << 20); err != nil {
		return nil, err
	}
	file, _, err := c.Request.FormFile("image")
	if err != nil {
		return nil, errs.New(errs.CodeValidation, "image field is required")
	}
	return file, nil
}

// extendReadDeadline 把本次连接的读截止时间推后。
// 不支持 deadline 的 Writer（如测试 recorder）只记 warn：
// 全局 ReadTimeout 仍在，状态码不变。
func (h *Handler) extendReadDeadline(c *gin.Context, budget time.Duration) {
	err := http.NewResponseController(c.Writer).SetReadDeadline(h.clock.Now().Add(budget))
	if err != nil {
		h.logger.WarnContext(c.Request.Context(), "photo: cannot extend the read deadline",
			"error", err.Error(), "path", c.Request.URL.Path)
	}
}

// extendWriteDeadline 把本次连接的写截止时间推后，失败处理同上。
func (h *Handler) extendWriteDeadline(c *gin.Context, budget time.Duration) {
	err := http.NewResponseController(c.Writer).SetWriteDeadline(h.clock.Now().Add(budget))
	if err != nil {
		h.logger.WarnContext(c.Request.Context(), "photo: cannot extend the write deadline",
			"error", err.Error(), "path", c.Request.URL.Path)
	}
}

// principal 解析作用域与审计主体；未登录 401，解析失败按错误码回。
func (h *Handler) principal(c *gin.Context) (corescope.Scope, appaudit.Actor, bool) {
	session, ok := coreauth.FromContext(c.Request.Context())
	if !ok {
		fail(c, errs.New(errs.CodeUnauthorized, "authentication required"))
		return corescope.Scope{}, appaudit.Actor{}, false
	}
	scope, actor, err := h.authz.Authorize(c.Request.Context(), session.User)
	if err != nil {
		fail(c, err)
		return corescope.Scope{}, appaudit.Actor{}, false
	}
	return scope, actor, true
}

// photoView 把领域记录转成传输形状。
func photoView(rec corephoto.Record) oapi.Photo {
	return oapi.Photo{
		Id:        rec.ID,
		Sha256:    hex.EncodeToString(rec.SHA256[:]),
		Mime:      oapi.PhotoMime(rec.Mime),
		Width:     rec.Width,
		Height:    rec.Height,
		Bytes:     rec.Bytes,
		CreatedAt: rec.CreatedAt,
	}
}

// photoPageView 把一页记录转成传输形状。
func photoPageView(page appphotos.Page) oapi.PhotoPage {
	items := make([]oapi.Photo, len(page.Items))
	for i, rec := range page.Items {
		items[i] = photoView(rec)
	}
	return oapi.PhotoPage{Items: items, NextCursor: page.NextCursor}
}

// schoolDefaultView 把默认图引用转成传输形状。
// 无引用时 photo_id 为 null、etag 为空串。
func schoolDefaultView(v appphotos.SchoolDefaultPhoto) oapi.SchoolDefaultPhoto {
	out := oapi.SchoolDefaultPhoto{Etag: v.ETag}
	if v.PhotoID != "" {
		id := v.PhotoID
		out.PhotoId = &id
	}
	return out
}

// fail 写标准错误信封。
func fail(c *gin.Context, err error) {
	respond.Error(c.Writer, c.Request, err)
}
