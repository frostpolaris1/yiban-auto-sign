// Package audit 把审计只读列表映射到 HTTP。
//
// 功能：GET /api/audit-logs（master/admin）。
// 本包**不**写 SQL、**不**校验链、**不**导出。
//
// 归属：entry/http 入口层。用例来自 app/audit；
// 角色是运行时门：user 一律 403。
//
// 口径：actor 过滤值按写入口径掩码；detail 已在用例出口脱敏。
// 游标由仓储编解码，处理器只透传。

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

// Handler 处理审计列表端点。
type Handler struct {
	reader Reader
}

// NewHandler 构造审计列表处理器。
func NewHandler(reader Reader) *Handler { return &Handler{reader: reader} }

// AuditLogList 实现 GET /api/audit-logs。
func (h *Handler) AuditLogList(c *gin.Context, params oapi.AuditLogListParams) {
	session, ok := coreauth.FromContext(c.Request.Context())
	if !ok || session.User.Status != coreauth.StatusActive {
		fail(c, errs.New(errs.CodeUnauthorized, "authentication required"))
		return
	}
	if session.User.Role != coreauth.RoleMaster && session.User.Role != coreauth.RoleAdmin {
		fail(c, errs.New(errs.CodeForbidden, "master or admin role is required"))
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

func deref(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func fail(c *gin.Context, err error) {
	respond.Error(c.Writer, c.Request, err)
}
