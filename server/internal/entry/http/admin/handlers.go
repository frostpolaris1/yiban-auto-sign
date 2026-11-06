// Package admin 把管理员绑定用例映射到 HTTP。
//
// # 功能
// 列绑定、绑定、解绑三个端点。
// 本包**不**写 SQL、**不**判角色、**不**解析作用域（由 app/schooladmin 负责）。
//
// # 归属
// entry/http 入口层。路由把 /api/schools/{id}/admins 交给本包。
//
// # 复用
// Handler、NewHandler；Service、Authorizer 是装配接缝。
//
// # 口径
// 绑定成功回 201；解绑成功回 204。
// 绑定与解绑都改变谁能读一所学校的数据，是危险操作：先验二次口令
// （acting user 自己的登录口令）再进用例，失败零写入。
// 绑定页展示**真实邮箱**；只有审计与日志使用掩码邮箱。
// 请求体解码失败走 respond.BindError：超限回 413，其余回 400。
package admin

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"net/http"
	"strconv"

	"github.com/gin-gonic/gin"
	openapi_types "github.com/oapi-codegen/runtime/types"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/auth"
	appadmin "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schooladmin"
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

// Reauth 是危险操作二次口令的窄接口（实现在 app/auth.Service）。
// userID 是会话主体；password 是它重新提交的登录口令。
// 缺值 400、错误 403、连续失败 429 都由错误码经 respond.Error 映射。
type Reauth interface {
	Reauthenticate(ctx context.Context, userID, password string) error
}

// Handler 处理 /api/schools/{id}/admins 下的端点。
type Handler struct {
	svc    Service
	authz  Authorizer
	reauth Reauth
}

// NewHandler 构造管理员处理器。
func NewHandler(svc Service, authz Authorizer) *Handler {
	return &Handler{svc: svc, authz: authz}
}

// WithReauth 注入危险操作二次口令用例。
// 未注入时绑定与解绑按 internal 拒绝（失败关闭）。
func (h *Handler) WithReauth(r Reauth) *Handler {
	h.reauth = r
	return h
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
// 绑定给目标用户开通一所学校的读面，故要求二次口令。
func (h *Handler) Add(c *gin.Context, id oapi.SchoolID, _ oapi.SchoolAdminAddParams) {
	scope, actor, ok := h.principal(c)
	if !ok {
		return
	}
	var body oapi.SchoolAdminCreate
	if !decode(c, &body) {
		return
	}
	if !h.reauthenticate(c, actor.UserID, body.CurrentPassword) {
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
// 解绑收回该校读面，故与绑定同级：要求二次口令。
func (h *Handler) Remove(c *gin.Context, id oapi.SchoolID, uid oapi.UserID, _ oapi.SchoolAdminRemoveParams) {
	scope, actor, ok := h.principal(c)
	if !ok {
		return
	}
	var body oapi.SchoolAdminRemoveRequest
	if !decode(c, &body) {
		return
	}
	if !h.reauthenticate(c, actor.UserID, body.CurrentPassword) {
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

func decode(c *gin.Context, target any) bool {
	if err := json.NewDecoder(c.Request.Body).Decode(target); err != nil {
		respond.BindError(c.Writer, c.Request, err)
		return false
	}
	return true
}

// reauthenticate 校验危险操作的二次口令并写出失败响应。
// 缺值（nil 或空串）回 400；用例错误按码映射（403/429/500）；
// 节流命中时先写 Retry-After 头再回 429。
// 返回 false 表示已写响应，调用方直接返回。
func (h *Handler) reauthenticate(c *gin.Context, userID string, password *string) bool {
	if h.reauth == nil {
		// 生产装配必须注入；缺失是接线错误，失败关闭。
		fail(c, errs.New(errs.CodeInternal, "reauthentication is not wired"))
		return false
	}
	value := ""
	if password != nil {
		value = *password
	}
	if value == "" {
		fail(c, errs.New(errs.CodeValidation, "current_password is required"))
		return false
	}
	if err := h.reauth.Reauthenticate(c.Request.Context(), userID, value); err != nil {
		writeRetryAfter(c, err)
		fail(c, err)
		return false
	}
	return true
}

// writeRetryAfter 在节流错误上写 Retry-After 头（秒，向上取整，至少 1）。
// 与账号变更限流的 429 响应口径一致。
func writeRetryAfter(c *gin.Context, err error) {
	var throttled *appauth.ReauthThrottled
	if !errors.As(err, &throttled) {
		return
	}
	seconds := int(math.Ceil(throttled.RetryAfter.Seconds()))
	if seconds < 1 {
		seconds = 1
	}
	c.Header("Retry-After", strconv.Itoa(seconds))
}

func fail(c *gin.Context, err error) {
	respond.Error(c.Writer, c.Request, err)
}
