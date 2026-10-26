// Package location 把定位策略用例映射到 HTTP。
//
// # 功能
// PUT /api/accounts/{id}/location 一个端点。
// 本包**不**写 SQL、**不**做围栏几何、**不**决定可见面。
//
// # 归属
// entry/http 入口层。作用域由 Authorizer（app/admin）解析，
// 可见面判据在 app/location（复用 app/account 的口径）。
//
// # 复用
// Handler、NewHandler；Service、Authorizer。
//
// # 口径
// 响应回显完整策略：auto 的 lng/lat 为 null，fixed 恒有值。
// reason 原文进库不进审计；审计只记 mode 与 has_reason。
package location

import (
	"context"
	"encoding/json"
	"net/http"

	"github.com/gin-gonic/gin"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	applocation "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/location"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// Service 是定位策略用例端口（实现为 app/location.Service）。
type Service interface {
	Set(ctx context.Context, scope corescope.Scope, accountID string, in applocation.Input, actor appaudit.Actor) (contract.Location, error)
}

// Authorizer 把会话主体解析成作用域与审计主体。
type Authorizer interface {
	Authorize(ctx context.Context, user coreauth.User) (corescope.Scope, appaudit.Actor, error)
}

// Handler 处理 /api/accounts/{id}/location。
type Handler struct {
	svc   Service
	authz Authorizer
}

// NewHandler 构造定位策略处理器。
func NewHandler(svc Service, authz Authorizer) *Handler {
	return &Handler{svc: svc, authz: authz}
}

// SetLocation 实现 PUT /api/accounts/{id}/location。
// 可见面由用例判定：跨校、跨用户与已软删一律 404。
func (h *Handler) SetLocation(c *gin.Context, id oapi.AccountID, _ oapi.AccountLocationSetParams) {
	session, ok := h.principal(c)
	if !ok {
		return
	}
	scope, actor, err := h.authz.Authorize(c.Request.Context(), session.User)
	if err != nil {
		fail(c, err)
		return
	}
	var body oapi.AccountLocationUpdate
	if err := json.NewDecoder(c.Request.Body).Decode(&body); err != nil {
		respond.BindError(c.Writer, c.Request, err)
		return
	}
	in := applocation.Input{Mode: string(body.Mode), Lng: body.Lng, Lat: body.Lat}
	if body.Reason != nil {
		in.Reason = *body.Reason
	}
	out, err := h.svc.Set(c.Request.Context(), scope, string(id), in, actor)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, locationResponse(out))
}

// locationResponse 把领域值转成传输形状。
// auto 行不带坐标：两个字段落 null，客户端据此区分。
func locationResponse(loc contract.Location) oapi.AccountLocation {
	out := oapi.AccountLocation{
		Mode:   oapi.AccountLocationMode(loc.Mode),
		Reason: loc.Reason,
	}
	if loc.Mode == contract.LocationFixed {
		lng, lat := loc.Lng, loc.Lat
		out.Lng, out.Lat = &lng, &lat
	}
	return out
}

// principal 解析会话；未登录 401。
func (h *Handler) principal(c *gin.Context) (coreauth.Session, bool) {
	session, ok := coreauth.FromContext(c.Request.Context())
	if !ok {
		fail(c, errs.New(errs.CodeUnauthorized, "authentication required"))
		return coreauth.Session{}, false
	}
	return session, true
}

// fail 写标准错误信封。
func fail(c *gin.Context, err error) {
	respond.Error(c.Writer, c.Request, err)
}
