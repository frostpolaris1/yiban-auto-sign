// Package user 把用户用例映射到 HTTP。
//
// # 功能
// 实现用户域全部端点：POST /api/users、GET /api/users（含 include_deleted）、
// PATCH /api/users/{id}、POST /api/users/{id}/password-reset、
// DELETE /api/users/{id}、POST /api/users/{id}/restore、GET /api/users/lookup。
// 解析会话与请求，调用 app/user，写出契约响应。
// 本包**不**写 SQL、**不**判角色白名单、**不**算口令哈希。
//
// # 归属
// entry/http 入口层。作用域由 Authorizer（app/admin）解析，
// 本层不重新解释 403 与 404。
//
// # 复用
// Handler、NewHandler；Service、Authorizer 是装配接缝。
//
// # 口径
// 响应只给契约 User 的字段集；口令与哈希**不出本层**。
// temp_password 只在 password-reset 的响应出现一次，本层不留副本。
// 列表与 lookup 的 items 空页输出 []，不是 null。
// 错误一律走 respond.Error，形状由信封包保证。

package user

import (
	"context"
	"encoding/json"
	"net/http"

	"github.com/gin-gonic/gin"
	openapi_types "github.com/oapi-codegen/runtime/types"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/user"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
)

// Service 是用户用例端口。
type Service interface {
	Create(ctx context.Context, scope corescope.Scope, in appuser.CreateInput, actor appaudit.Actor) (appuser.Record, error)
	List(ctx context.Context, scope corescope.Scope, filter appuser.ListFilter, cursor string, limit int) (appuser.Page, error)
	Update(ctx context.Context, scope corescope.Scope, id string, in appuser.UpdateInput, actor appaudit.Actor) (appuser.Record, error)
	PasswordReset(ctx context.Context, scope corescope.Scope, id string, actor appaudit.Actor) (string, error)
	Delete(ctx context.Context, scope corescope.Scope, id string, actor appaudit.Actor) error
	Restore(ctx context.Context, scope corescope.Scope, id string, actor appaudit.Actor) (appuser.Record, error)
	Lookup(ctx context.Context, scope corescope.Scope, query string, limit int, exact bool) ([]appuser.LookupRef, error)
}

// Authorizer 把会话主体解析成作用域与审计主体。
type Authorizer interface {
	Authorize(ctx context.Context, user coreauth.User) (corescope.Scope, appaudit.Actor, error)
}

// Handler 处理 /api/users。
type Handler struct {
	svc   Service
	authz Authorizer
}

// NewHandler 构造用户处理器。
func NewHandler(svc Service, authz Authorizer) *Handler {
	return &Handler{svc: svc, authz: authz}
}

// Create 实现 POST /api/users。
func (h *Handler) Create(c *gin.Context, _ oapi.UserCreateParams) {
	scope, actor, ok := h.principal(c)
	if !ok {
		return
	}
	var body oapi.UserCreate
	if !decode(c, &body) {
		return
	}
	record, err := h.svc.Create(c.Request.Context(), scope, appuser.CreateInput{
		Email:    string(body.Email),
		Password: body.Password,
		Role:     coreauth.Role(body.Role),
	}, actor)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusCreated, userResponse(record))
}

// List 实现 GET /api/users。
func (h *Handler) List(c *gin.Context, params oapi.UserListParams) {
	scope, _, ok := h.principal(c)
	if !ok {
		return
	}
	limit, err := app.PageLimit(params.Limit)
	if err != nil {
		fail(c, err)
		return
	}
	filter := appuser.ListFilter{}
	if params.Role != nil {
		filter.Role = coreauth.Role(*params.Role)
	}
	if params.Status != nil {
		filter.Status = coreauth.Status(*params.Status)
	}
	if params.Q != nil {
		filter.Query = *params.Q
	}
	if params.IncludeDeleted != nil {
		filter.IncludeDeleted = *params.IncludeDeleted
	}
	page, err := h.svc.List(c.Request.Context(), scope, filter, cursorOf(params.Cursor), limit)
	if err != nil {
		fail(c, err)
		return
	}
	items := make([]oapi.User, 0, len(page.Items))
	for _, record := range page.Items {
		items = append(items, userResponse(record))
	}
	respond.JSON(c.Writer, http.StatusOK, oapi.UserPage{Items: items, NextCursor: page.NextCursor})
}

// Update 实现 PATCH /api/users/{id}。
func (h *Handler) Update(c *gin.Context, id string, _ oapi.UserUpdateParams) {
	scope, actor, ok := h.principal(c)
	if !ok {
		return
	}
	var body oapi.UserUpdate
	if !decode(c, &body) {
		return
	}
	in := appuser.UpdateInput{}
	if body.Role != nil {
		role := coreauth.Role(*body.Role)
		in.Role = &role
	}
	if body.Status != nil {
		status := coreauth.Status(*body.Status)
		in.Status = &status
	}
	record, err := h.svc.Update(c.Request.Context(), scope, id, in, actor)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, userResponse(record))
}

// PasswordReset 实现 POST /api/users/{id}/password-reset。
// temp_password 只出现在本次响应；本层不留副本、不写日志。
func (h *Handler) PasswordReset(c *gin.Context, id string, _ oapi.UserPasswordResetParams) {
	scope, actor, ok := h.principal(c)
	if !ok {
		return
	}
	temp, err := h.svc.PasswordReset(c.Request.Context(), scope, id, actor)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, oapi.UserTempPassword{TempPassword: temp})
}

// Delete 实现 DELETE /api/users/{id}。
func (h *Handler) Delete(c *gin.Context, id string, _ oapi.UserDeleteParams) {
	scope, actor, ok := h.principal(c)
	if !ok {
		return
	}
	if err := h.svc.Delete(c.Request.Context(), scope, id, actor); err != nil {
		fail(c, err)
		return
	}
	c.Status(http.StatusNoContent)
}

// Restore 实现 POST /api/users/{id}/restore。
func (h *Handler) Restore(c *gin.Context, id string, _ oapi.UserRestoreParams) {
	scope, actor, ok := h.principal(c)
	if !ok {
		return
	}
	record, err := h.svc.Restore(c.Request.Context(), scope, id, actor)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, userResponse(record))
}

// Lookup 实现 GET /api/users/lookup。
func (h *Handler) Lookup(c *gin.Context, params oapi.UserLookupParams) {
	scope, _, ok := h.principal(c)
	if !ok {
		return
	}
	limit := appuser.LookupDefaultLimit
	if params.Limit != nil {
		limit = *params.Limit
	}
	exact := params.Exact != nil && *params.Exact
	refs, err := h.svc.Lookup(c.Request.Context(), scope, params.Q, limit, exact)
	if err != nil {
		fail(c, err)
		return
	}
	items := make([]oapi.UserLookupRef, 0, len(refs))
	for _, ref := range refs {
		items = append(items, oapi.UserLookupRef{
			Id:          ref.ID,
			EmailMasked: ref.EmailMasked,
			Status:      oapi.UserStatus(ref.Status),
		})
	}
	respond.JSON(c.Writer, http.StatusOK, oapi.UserLookupPage{Items: items})
}

// principal 取会话并解析作用域。失败时已写响应。
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

// userResponse 把用户行映射成契约 shape。
// 邮箱已归一；last_login_at 与 deleted_at 为 nil 时输出 null（契约要求键存在）。
func userResponse(record appuser.Record) oapi.User {
	return oapi.User{
		Id:                 record.ID,
		Email:              openapi_types.Email(record.Email),
		Role:               oapi.UserRole(record.Role),
		Status:             oapi.UserStatus(record.Status),
		MustChangePassword: record.MustChangePassword,
		CreatedAt:          record.CreatedAt,
		LastLoginAt:        record.LastLoginAt,
		DeletedAt:          record.DeletedAt,
	}
}

func decode(c *gin.Context, target any) bool {
	if err := json.NewDecoder(c.Request.Body).Decode(target); err != nil {
		respond.BindError(c.Writer, c.Request, err)
		return false
	}
	return true
}

// cursorOf 把可选游标取成字符串。缺省即首页。
func cursorOf(cursor *string) string {
	if cursor == nil {
		return ""
	}
	return *cursor
}

func fail(c *gin.Context, err error) {
	respond.Error(c.Writer, c.Request, err)
}
