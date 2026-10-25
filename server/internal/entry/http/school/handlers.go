// Package school 把院校用例映射到 HTTP。
//
// # 功能
// 解析会话与请求体，调用 app/school，写出契约响应。
// 本包**不**写 SQL、**不**判状态迁移、**不**加密码。
//
// # 归属
// entry/http 入口层。不要在 handler 里改作用域：
// 作用域只由 Authorizer（app/admin）解析。
//
// # 复用
// Handler、NewHandler；Service、Authorizer 是测试与装配的接缝。
//
// # 口径
// 错误一律走 respond.Error，形状由信封包保证。
// 跨校 404 与无权 403 的判定在用例层，本层不重新解释。
package school

import (
	"context"
	"encoding/json"
	"net/http"

	"github.com/gin-gonic/gin"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/school"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
)

// Service 是院校用例端口。
type Service interface {
	List(ctx context.Context, scope corescope.Scope, cursor string, limit int) (appschool.Page, error)
	Create(ctx context.Context, scope corescope.Scope, in appschool.New, actor appaudit.Actor) (appschool.Record, error)
	Update(ctx context.Context, scope corescope.Scope, id string, in appschool.Update, actor appaudit.Actor) (appschool.Record, error)
}

// Authorizer 把会话主体解析成作用域与审计主体。
type Authorizer interface {
	Authorize(ctx context.Context, user coreauth.User) (corescope.Scope, appaudit.Actor, error)
}

// Handler 处理 /api/schools 下的全部端点。
type Handler struct {
	svc   Service
	authz Authorizer
}

// NewHandler 构造院校处理器。
func NewHandler(svc Service, authz Authorizer) *Handler {
	return &Handler{svc: svc, authz: authz}
}

// List 实现 GET /api/schools。
func (h *Handler) List(c *gin.Context, params oapi.SchoolListParams) {
	scope, _, ok := h.principal(c)
	if !ok {
		return
	}
	limit, err := app.PageLimit(params.Limit)
	if err != nil {
		fail(c, err)
		return
	}
	page, err := h.svc.List(c.Request.Context(), scope, cursorOf(params.Cursor), limit)
	if err != nil {
		fail(c, err)
		return
	}
	items := make([]oapi.School, 0, len(page.Items))
	for _, record := range page.Items {
		items = append(items, schoolResponse(record))
	}
	respond.JSON(c.Writer, http.StatusOK, oapi.SchoolPage{Items: items, NextCursor: page.NextCursor})
}

// Create 实现 POST /api/schools。
func (h *Handler) Create(c *gin.Context, _ oapi.SchoolCreateParams) {
	scope, actor, ok := h.principal(c)
	if !ok {
		return
	}
	var body oapi.SchoolCreate
	if !decode(c, &body) {
		return
	}
	timeZone := ""
	if body.Timezone != nil {
		timeZone = string(*body.Timezone)
	}
	record, err := h.svc.Create(c.Request.Context(), scope, appschool.New{
		Code:     body.Code,
		Name:     body.Name,
		TimeZone: timeZone,
	}, actor)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusCreated, schoolResponse(record))
}

// Update 实现 PATCH /api/schools/{id}。
func (h *Handler) Update(c *gin.Context, id oapi.SchoolID, _ oapi.SchoolUpdateParams) {
	scope, actor, ok := h.principal(c)
	if !ok {
		return
	}
	var body oapi.SchoolUpdate
	if !decode(c, &body) {
		return
	}
	in := appschool.Update{Name: body.Name}
	if body.Status != nil {
		status := appschool.Status(*body.Status)
		in.Status = &status
	}
	if body.Timezone != nil {
		timeZone := string(*body.Timezone)
		in.TimeZone = &timeZone
	}
	record, err := h.svc.Update(c.Request.Context(), scope, id, in, actor)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, schoolResponse(record))
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

func schoolResponse(record appschool.Record) oapi.School {
	return oapi.School{
		Id:        record.ID,
		Code:      record.Code,
		Name:      record.Name,
		Status:    oapi.SchoolStatus(record.Status),
		Timezone:  record.TimeZone,
		CreatedAt: record.CreatedAt,
		UpdatedAt: record.UpdatedAt,
	}
}

func cursorOf(cursor *string) string {
	if cursor == nil {
		return ""
	}
	return *cursor
}

func decode(c *gin.Context, target any) bool {
	if err := json.NewDecoder(c.Request.Body).Decode(target); err != nil {
		fail(c, errs.New(errs.CodeValidation, "invalid request body"))
		return false
	}
	return true
}

func fail(c *gin.Context, err error) {
	respond.Error(c.Writer, c.Request, err)
}
