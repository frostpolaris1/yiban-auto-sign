// Package audit 把审计只读列表与链巡检映射到 HTTP。
//
// 功能：GET /api/audit-logs、GET /api/admin/audit-status（master/admin）。
// 本包**不**写 SQL、**不**自己校验链、**不**导出。
//
// 归属：entry/http 入口层。用例来自 app/audit；
// 角色是运行时门：列表只对 master/admin 开放，可见面由会话解析出的
// core/scope.Scope 决定——master 看全部；admin 只看绑定院校的行，
// school_id IS NULL 的行对 admin 不可见（空绑定即空页）；user 一律 403。
// 链巡检是 master 专属：admin 与 user 都 403。
//
// 口径：actor 过滤值按写入口径掩码；target 精确匹配目标 id；
// detail 已在用例出口脱敏。游标由仓储编解码，处理器只透传。
// 响应信封带 total（同过滤条件忽略游标）与 has_more（next_cursor 非空）。
// 巡检结论（含断链与锚点不符）一律 200：结论不是 HTTP 错误。

package audit

import (
	"context"
	"net/http"

	"github.com/gin-gonic/gin"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
)

// Reader 是审计列表用例端口。
type Reader interface {
	List(ctx context.Context, filter appaudit.ListFilter) (appaudit.LogPage, error)
}

// StatusReader 是审计链巡检用例端口。
type StatusReader interface {
	Status(ctx context.Context) (appaudit.ChainStatus, error)
}

// Authorizer 把已鉴权会话解析成可见面。入口层不复刻院校绑定判定，
// 只消费 app 层的授权结果。
type Authorizer interface {
	Authorize(ctx context.Context, user coreauth.User) (corescope.Scope, appaudit.Actor, error)
}

// Handler 处理审计只读列表与链巡检端点。
type Handler struct {
	reader Reader
	status StatusReader
	authz  Authorizer
}

// NewHandler 构造审计处理器。authz 是必需依赖：缺失时两个端点都
// fail-closed 回 500，而不是退化成无可见面的读取。
func NewHandler(reader Reader, authz Authorizer) *Handler {
	return &Handler{reader: reader, authz: authz}
}

// WithStatus 挂上巡检用例。未挂时巡检端点回 501。
func (h *Handler) WithStatus(status StatusReader) *Handler {
	h.status = status
	return h
}

// AuditLogList 实现 GET /api/audit-logs。
func (h *Handler) AuditLogList(c *gin.Context, params oapi.AuditLogListParams) {
	if h.authz == nil || h.reader == nil {
		fail(c, errs.New(errs.CodeInternal, "audit handler is not configured"))
		return
	}
	scope, ok := authorize(c, h.authz)
	if !ok {
		return
	}
	limit, err := app.PageLimit(params.Limit)
	if err != nil {
		fail(c, err)
		return
	}
	filter := appaudit.ListFilter{
		Action: deref(params.Action),
		Actor:  deref(params.Actor),
		Target: deref(params.Target),
		From:   params.From,
		To:     params.To,
		Limit:  limit,
		Scope:  scope,
	}
	if params.Cursor != nil {
		filter.Cursor = *params.Cursor
	}
	page, err := h.reader.List(c.Request.Context(), filter)
	if err != nil {
		fail(c, err)
		return
	}
	items := make([]oapi.AuditLogItem, 0, len(page.Items))
	for _, row := range page.Items {
		items = append(items, oapi.AuditLogItem{
			Id:     row.ID,
			At:     row.TS,
			Action: row.Action,
			Actor:  row.ActorLabel,
			Target: row.Target,
			Detail: row.Detail,
		})
	}
	respond.JSON(c.Writer, http.StatusOK, oapi.AuditLogPage{
		Items:      items,
		NextCursor: page.NextCursor,
		Total:      page.Total,
		HasMore:    page.NextCursor != "",
	})
}

// AuditStatus 实现 GET /api/admin/audit-status。
// 链完好、断链与空链都答 200：三态是巡检结论，不是 HTTP 错误。
func (h *Handler) AuditStatus(c *gin.Context) {
	if h.authz == nil {
		fail(c, errs.New(errs.CodeInternal, "audit handler is not configured"))
		return
	}
	if !requireMaster(c) {
		return
	}
	if h.status == nil {
		respond.NotImplemented(c.Writer, c.Request)
		return
	}
	status, err := h.status.Status(c.Request.Context())
	if err != nil {
		fail(c, err)
		return
	}
	report := oapi.AuditStatusReport{
		Ok:          status.OK,
		Empty:       status.Empty,
		CheckedRows: status.CheckedRows,
		LastHash:    status.LastHash,
		BrokenAt:    status.BrokenAt,
		CheckedAt:   status.CheckedAt,
	}
	anchorStatus := oapi.AuditStatusReportAnchorStatus(status.AnchorStatus)
	if !anchorStatus.Valid() {
		anchorStatus = oapi.AuditStatusReportAnchorStatusDisabled
	}
	report.AnchorStatus = &anchorStatus
	anchorOK := status.AnchorOK
	report.AnchorOk = &anchorOK
	if status.AnchorReason != "" {
		report.AnchorReason = &status.AnchorReason
	}
	if status.AnchorSeq > 0 {
		report.AnchorSeq = &status.AnchorSeq
	}
	if status.AnchorCount > 0 {
		report.AnchorCount = &status.AnchorCount
	}
	respond.JSON(c.Writer, http.StatusOK, report)
}

// authorize 解析会话可见面，并只放行 master/admin 两种可见面。
// user 与非 active 会话都在这里被挡下；授权失败不触达读取端口。
func authorize(c *gin.Context, authz Authorizer) (corescope.Scope, bool) {
	session, ok := coreauth.FromContext(c.Request.Context())
	if !ok || session.User.Status != coreauth.StatusActive {
		fail(c, errs.New(errs.CodeUnauthorized, "authentication required"))
		return corescope.Scope{}, false
	}
	scope, _, err := authz.Authorize(c.Request.Context(), session.User)
	if err != nil {
		fail(c, err)
		return corescope.Scope{}, false
	}
	if scope.Kind() != corescope.KindMaster && scope.Kind() != corescope.KindAdmin {
		fail(c, errs.New(errs.CodeForbidden, "master or admin role is required"))
		return corescope.Scope{}, false
	}
	return scope, true
}

// requireMaster 是链巡检的角色门：只放行有效会话的 master。
func requireMaster(c *gin.Context) bool {
	session, ok := coreauth.FromContext(c.Request.Context())
	if !ok || session.User.Status != coreauth.StatusActive {
		fail(c, errs.New(errs.CodeUnauthorized, "authentication required"))
		return false
	}
	if session.User.Role != coreauth.RoleMaster {
		fail(c, errs.New(errs.CodeForbidden, "master role is required"))
		return false
	}
	return true
}

func deref(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func fail(c *gin.Context, err error) {
	respond.Error(c.Writer, c.Request, err)
}
