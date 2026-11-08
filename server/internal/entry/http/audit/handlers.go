// Package audit 把审计只读列表与链巡检映射到 HTTP。
//
// 功能：GET /api/audit-logs、GET /api/admin/audit-status（master/admin）。
// 本包**不**写 SQL、**不**自己校验链、**不**导出。
//
// 归属：entry/http 入口层。用例来自 app/audit；
// 角色是运行时门：user 一律 403。
//
// 口径：actor 过滤值按写入口径掩码；detail 已在用例出口脱敏。
// 游标由仓储编解码，处理器只透传。
// 巡检结论（含断链）一律 200：断链是结论，不是 HTTP 错误。
// 旧线 parity：master 与 admin 都看全局审计，**不按院校作用域收窄**
// （旧 web/routes/audit_api.py 同为全局视图）。这是已接受的风险，
// 与「admin 只见绑定校」的一般规则刻意不同。

package audit

import (
	"context"
	"net/http"

	"github.com/gin-gonic/gin"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
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

// Handler 处理审计只读列表与链巡检端点。
type Handler struct {
	reader Reader
	status StatusReader
}

// NewHandler 构造审计列表处理器。
func NewHandler(reader Reader) *Handler { return &Handler{reader: reader} }

// WithStatus 挂上巡检用例。未挂时巡检端点回 501。
func (h *Handler) WithStatus(status StatusReader) *Handler {
	h.status = status
	return h
}

// AuditLogList 实现 GET /api/audit-logs。
func (h *Handler) AuditLogList(c *gin.Context, params oapi.AuditLogListParams) {
	if !requireAdmin(c) {
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
		From:   params.From,
		To:     params.To,
		Limit:  limit,
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
			Detail: row.Detail,
		})
	}
	respond.JSON(c.Writer, http.StatusOK, oapi.AuditLogPage{
		Items:      items,
		NextCursor: page.NextCursor,
	})
}

// AuditStatus 实现 GET /api/admin/audit-status。
// 链完好、断链与空链都答 200：三态是巡检结论，不是 HTTP 错误。
func (h *Handler) AuditStatus(c *gin.Context) {
	if !requireAdmin(c) {
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
	respond.JSON(c.Writer, http.StatusOK, oapi.AuditStatusReport{
		Ok:          status.OK,
		Empty:       status.Empty,
		CheckedRows: status.CheckedRows,
		LastHash:    status.LastHash,
		BrokenAt:    status.BrokenAt,
		CheckedAt:   status.CheckedAt,
	})
}

// requireAdmin 是两条审计端点的共同角色门：只放行有效会话的 master/admin。
func requireAdmin(c *gin.Context) bool {
	session, ok := coreauth.FromContext(c.Request.Context())
	if !ok || session.User.Status != coreauth.StatusActive {
		fail(c, errs.New(errs.CodeUnauthorized, "authentication required"))
		return false
	}
	if session.User.Role != coreauth.RoleMaster && session.User.Role != coreauth.RoleAdmin {
		fail(c, errs.New(errs.CodeForbidden, "master or admin role is required"))
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
