// Package account 把账号用例映射到 HTTP。
//
// # 功能
// 列表、创建、读取、修改、审核、暂停、软删七个端点。
// 本包**不**写 SQL、**不**判状态迁移、**不**加密凭据。
//
// # 归属
// entry/http 入口层。作用域由 Authorizer（app/admin）解析。
//
// # 复用
// Handler、NewHandler；Service、Authorizer 是装配接缝。
//
// # 口径
// 响应只给 phone_masked；原始手机号在 Record 里，**不出本层**。
// 口令与设备码只进请求体，本层不记录、不回显。
// phone_model 在 P1 存在凭据密文里，契约的 Account 响应不声明该字段。
package account

import (
	"context"
	"encoding/json"
	"net/http"

	"github.com/gin-gonic/gin"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
)

// Service 是账号用例端口。
type Service interface {
	List(ctx context.Context, scope corescope.Scope, filter appaccount.ListFilter, cursor string, limit int) (appaccount.Page, error)
	Create(ctx context.Context, scope corescope.Scope, in appaccount.CreateInput, actor appaudit.Actor) (appaccount.Record, error)
	Get(ctx context.Context, scope corescope.Scope, id string) (appaccount.Record, error)
	Update(ctx context.Context, scope corescope.Scope, id string, in appaccount.UpdateInput, actor appaudit.Actor) (appaccount.Record, error)
	Review(ctx context.Context, scope corescope.Scope, id string, approve bool, reason string, actor appaudit.Actor) (appaccount.Record, error)
	Pause(ctx context.Context, scope corescope.Scope, id string, paused bool, actor appaudit.Actor) (appaccount.Record, error)
	Delete(ctx context.Context, scope corescope.Scope, id string, actor appaudit.Actor) error
}

// Authorizer 把会话主体解析成作用域与审计主体。
type Authorizer interface {
	Authorize(ctx context.Context, user coreauth.User) (corescope.Scope, appaudit.Actor, error)
}

// Handler 处理 /api/accounts 下的全部端点。
type Handler struct {
	svc   Service
	authz Authorizer
}

// NewHandler 构造账号处理器。
func NewHandler(svc Service, authz Authorizer) *Handler {
	return &Handler{svc: svc, authz: authz}
}

// List 实现 GET /api/accounts。
func (h *Handler) List(c *gin.Context, params oapi.AccountListParams) {
	scope, _, ok := h.principal(c)
	if !ok {
		return
	}
	limit, err := app.PageLimit(params.Limit)
	if err != nil {
		fail(c, err)
		return
	}
	filter := appaccount.ListFilter{}
	if params.SchoolId != nil {
		filter.SchoolID = *params.SchoolId
	}
	if params.Status != nil {
		filter.Status = appaccount.Status(*params.Status)
	}
	if params.Q != nil {
		filter.Query = *params.Q
	}
	page, err := h.svc.List(c.Request.Context(), scope, filter, cursorOf(params.Cursor), limit)
	if err != nil {
		fail(c, err)
		return
	}
	items := make([]oapi.Account, 0, len(page.Items))
	for _, record := range page.Items {
		items = append(items, accountResponse(record))
	}
	respond.JSON(c.Writer, http.StatusOK, oapi.AccountPage{Items: items, NextCursor: page.NextCursor})
}

// Create 实现 POST /api/accounts。
func (h *Handler) Create(c *gin.Context, _ oapi.AccountCreateParams) {
	scope, actor, ok := h.principal(c)
	if !ok {
		return
	}
	var body oapi.AccountCreate
	if !decode(c, &body) {
		return
	}
	in := appaccount.CreateInput{
		SchoolID:   body.SchoolId,
		Phone:      body.Phone,
		Password:   body.Password,
		DeviceCode: stringOf(body.DeviceCode),
		PhoneModel: stringOf(body.PhoneModel),
	}
	if body.OwnerUserId != nil {
		in.OwnerUserID = *body.OwnerUserId
	}
	record, err := h.svc.Create(c.Request.Context(), scope, in, actor)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusCreated, accountResponse(record))
}

// Get 实现 GET /api/accounts/{id}。
func (h *Handler) Get(c *gin.Context, id oapi.AccountID) {
	scope, _, ok := h.principal(c)
	if !ok {
		return
	}
	record, err := h.svc.Get(c.Request.Context(), scope, id)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, accountResponse(record))
}

// Update 实现 PATCH /api/accounts/{id}。
func (h *Handler) Update(c *gin.Context, id oapi.AccountID, _ oapi.AccountUpdateParams) {
	scope, actor, ok := h.principal(c)
	if !ok {
		return
	}
	var body oapi.AccountUpdate
	if !decode(c, &body) {
		return
	}
	record, err := h.svc.Update(c.Request.Context(), scope, id, appaccount.UpdateInput{
		Phone:      body.Phone,
		Password:   body.Password,
		DeviceCode: body.DeviceCode,
		PhoneModel: body.PhoneModel,
	}, actor)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, accountResponse(record))
}

// Delete 实现 DELETE /api/accounts/{id}。
func (h *Handler) Delete(c *gin.Context, id oapi.AccountID, _ oapi.AccountDeleteParams) {
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

// Review 实现 POST /api/accounts/{id}/review。
func (h *Handler) Review(c *gin.Context, id oapi.AccountID, _ oapi.AccountReviewParams) {
	scope, actor, ok := h.principal(c)
	if !ok {
		return
	}
	var body oapi.AccountReviewRequest
	if !decode(c, &body) {
		return
	}
	approve := false
	switch string(body.Decision) {
	case "approve":
		approve = true
	case "reject":
		approve = false
	default:
		fail(c, errs.New(errs.CodeValidation, "decision must be approve or reject"))
		return
	}
	record, err := h.svc.Review(c.Request.Context(), scope, id, approve, stringOf(body.Reason), actor)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, accountResponse(record))
}

// Pause 实现 POST /api/accounts/{id}/pause。
// paused 是必填布尔值，故用指针解码区分「缺字段」与 false。
func (h *Handler) Pause(c *gin.Context, id oapi.AccountID, _ oapi.AccountPauseParams) {
	scope, actor, ok := h.principal(c)
	if !ok {
		return
	}
	var body struct {
		Paused *bool `json:"paused"`
	}
	if !decode(c, &body) {
		return
	}
	if body.Paused == nil {
		fail(c, errs.New(errs.CodeValidation, "paused is required"))
		return
	}
	record, err := h.svc.Pause(c.Request.Context(), scope, id, *body.Paused, actor)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, accountResponse(record))
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

func accountResponse(record appaccount.Record) oapi.Account {
	out := oapi.Account{
		Id:          record.ID,
		SchoolId:    record.SchoolID,
		OwnerUserId: record.OwnerUserID,
		PhoneMasked: mask.MaskPhone(record.Phone),
		Status:      oapi.AccountStatus(record.Status),
		// 拒绝理由是自由文本；写库时保留原文，**出口一律掩码**。
		// 审核、读取、列表三条响应同源，故只在这里处理一次。
		RejectReason: mask.MaskText(record.RejectReason),
		ReviewedAt:   record.ReviewedAt,
		CreatedAt:    record.CreatedAt,
		UpdatedAt:    record.UpdatedAt,
	}
	if record.ReviewedBy != "" {
		reviewedBy := record.ReviewedBy
		out.ReviewedBy = &reviewedBy
	}
	return out
}

func cursorOf(cursor *string) string {
	if cursor == nil {
		return ""
	}
	return *cursor
}

func stringOf(value *string) string {
	if value == nil {
		return ""
	}
	return *value
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
