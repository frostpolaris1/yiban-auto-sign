// Package admin 把管理员绑定用例映射到 HTTP。
//
// # 功能
// 列绑定、绑定、解绑三个端点。
// 本包**不**写 SQL、**不**判角色、**不**解析作用域（由 app/admin 负责）。
//
// # 归属
// entry/http 入口层。路由把 /api/schools/{id}/admins 交给本包。
//
// # 复用
// Handler、NewHandler；Service、Authorizer 是装配接缝。
//
// # 口径
// 绑定成功回 201；解绑成功回 204。
// 邮箱在数据源里已是掩码后的展示值吗？**不是**：绑定页展示真实邮箱，
// 只有审计与日志使用掩码邮箱。
package admin

import (
	"context"
	"encoding/json"
	"net/http"

	"github.com/gin-gonic/gin"
	openapi_types "github.com/oapi-codegen/runtime/types"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appadmin "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/admin"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
)

// Service 是管理员绑定用例端口。
type Service interface {
	List(ctx context.Context, scope corescope.Scope, schoolID, cursor string, limit int) (appadmin.Page, error)
	Grant(ctx context.Context, scope corescope.Scope, schoolID, userID string, actor appaudit.Actor) (appadmin.Binding, error)
	Revoke(ctx context.Context, scope corescope.Scope, schoolID, userID string, actor appaudit.Actor) error
}

// Authorizer 把会话主体解析成作用域与审计主体。
type Authorizer interface {
	Authorize(ctx context.Context, user coreauth.User) (corescope.Scope, appaudit.Actor, error)
}

// Handler 处理 /api/schools/{id}/admins 下的端点。
type Handler struct {
	svc   Service
	authz Authorizer
}

// NewHandler 构造管理员处理器。
func NewHandler(svc Service, authz Authorizer) *Handler {
	return &Handler{svc: svc, authz: authz}
}

// List 实现 GET /api/schools/{id}/admins。
func (h *Handler) List(c *gin.Context, id oapi.SchoolID, params oapi.SchoolAdminListParams) {
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
		cursor = *params.Cursor
	}
	page, err := h.svc.List(c.Request.Context(), scope, id, cursor, limit)
	if err != nil {
		fail(c, err)
		return
	}
	items := make([]oapi.SchoolAdmin, 0, len(page.Items))
	for _, binding := range page.Items {
		items = append(items, bindingResponse(binding))
	}
	respond.JSON(c.Writer, http.StatusOK, oapi.SchoolAdminPage{Items: items, NextCursor: page.NextCursor})
}

// Add 实现 POST /api/schools/{id}/admins。
func (h *Handler) Add(c *gin.Context, id oapi.SchoolID, _ oapi.SchoolAdminAddParams) {
	scope, actor, ok := h.principal(c)
	if !ok {
		return
	}
	var body oapi.SchoolAdminCreate
	if err := json.NewDecoder(c.Request.Body).Decode(&body); err != nil {
		fail(c, errs.New(errs.CodeValidation, "invalid request body"))
		return
	}
	binding, err := h.svc.Grant(c.Request.Context(), scope, id, body.UserId, actor)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusCreated, bindingResponse(binding))
}

// Remove 实现 DELETE /api/schools/{id}/admins/{uid}。
func (h *Handler) Remove(c *gin.Context, id oapi.SchoolID, uid oapi.UserID, _ oapi.SchoolAdminRemoveParams) {
	scope, actor, ok := h.principal(c)
	if !ok {
		return
	}
	if err := h.svc.Revoke(c.Request.Context(), scope, id, uid, actor); err != nil {
		fail(c, err)
		return
	}
	c.Status(http.StatusNoContent)
}

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

func bindingResponse(binding appadmin.Binding) oapi.SchoolAdmin {
	return oapi.SchoolAdmin{
		UserId:    binding.UserID,
		Email:     openapi_types.Email(binding.Email),
		Role:      oapi.UserRole(binding.Role),
		GrantedBy: binding.GrantedBy,
		CreatedAt: binding.CreatedAt,
	}
}

func fail(c *gin.Context, err error) {
	respond.Error(c.Writer, c.Request, err)
}
