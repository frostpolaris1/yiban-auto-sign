// Package user 把用户用例映射到 HTTP。
//
// # 功能
// 只实现 POST /api/users：解析会话与请求体，调用 app/user，写出契约响应。
// 本包**不**写 SQL、**不**判角色白名单、**不**算口令哈希。
//
// # 归属
// entry/http 入口层。作用域由 Authorizer（app/admin）解析，
// 本层不重新解释 403 与 400。
//
// # 复用
// Handler、NewHandler；Service、Authorizer 是装配接缝。
//
// # 口径
// 响应只给契约 User 的七个字段；口令与哈希**不出本层**。
// 错误一律走 respond.Error，形状由信封包保证。

package user

import (
	"context"
	"encoding/json"
	"net/http"

	"github.com/gin-gonic/gin"
	openapi_types "github.com/oapi-codegen/runtime/types"

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
// 邮箱已归一；last_login_at 缺省时省略。
func userResponse(record appuser.Record) oapi.User {
	return oapi.User{
		Id:                 record.ID,
		Email:              openapi_types.Email(record.Email),
		Role:               oapi.UserRole(record.Role),
		Status:             oapi.UserStatus(record.Status),
		MustChangePassword: record.MustChangePassword,
		CreatedAt:          record.CreatedAt,
		LastLoginAt:        record.LastLoginAt,
	}
}

func decode(c *gin.Context, target any) bool {
	if err := json.NewDecoder(c.Request.Body).Decode(target); err != nil {
		respond.BindError(c.Writer, c.Request, err)
		return false
	}
	return true
}

func fail(c *gin.Context, err error) {
	respond.Error(c.Writer, c.Request, err)
}
