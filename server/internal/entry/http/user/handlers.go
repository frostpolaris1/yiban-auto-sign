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
// entry/http 入口层。作用域由 Authorizer（app/schooladmin）解析，
// 本层不重新解释 403 与 404。
//
// # 复用
// Handler、NewHandler；Service、Authorizer 是装配接缝。
//
// # 口径
// 响应只给契约 User 的字段集；口令与哈希**不出本层**。
// temp_password 只在 password-reset 的响应出现一次，本层不留副本。
// 改角色/状态、删用户、重置口令、恢复用户与新建用户（可建 master）
// 是危险操作：先验二次口令（acting user 自己的登录口令）再进用例，
// 失败零写入。
// 列表与 lookup 的 items 空页输出 []，不是 null。
// 错误一律走 respond.Error，形状由信封包保证。

package user

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
	appuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/user"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
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
	// MailNotify 与 SetMailNotify 只作用于调用者自己的开关；
	// 目标 id 由入口层取会话用户，不来自请求字段。
	MailNotify(ctx context.Context, userID string) (bool, error)
	SetMailNotify(ctx context.Context, userID string, enabled bool, actor appaudit.Actor) (bool, error)
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

// Handler 处理 /api/users。
type Handler struct {
	svc    Service
	authz  Authorizer
	reauth Reauth
}

// NewHandler 构造用户处理器。
func NewHandler(svc Service, authz Authorizer) *Handler {
	return &Handler{svc: svc, authz: authz}
}

// WithReauth 注入危险操作二次口令用例。
// 未注入时改角色/状态、删用户、重置口令、恢复与新建按 internal 拒绝（失败关闭）。
func (h *Handler) WithReauth(r Reauth) *Handler {
	h.reauth = r
	return h
}

// Create 实现 POST /api/users。
// 该端点可建 master/admin，故与改角色同级：总是要求二次口令。
func (h *Handler) Create(c *gin.Context, _ oapi.UserCreateParams) {
	scope, actor, ok := h.principal(c)
	if !ok {
		return
	}
	var body oapi.UserCreate
	if !decode(c, &body) {
		return
	}
	if !h.reauthenticate(c, actor.UserID, body.CurrentPassword) {
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
	if params.Group != nil {
		filter.Group = coreuser.Group(*params.Group)
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
	// 列表可变：ETag 只省响应体，鉴权与作用域判定已经完成。
	inm := ""
	if params.IfNoneMatch != nil {
		inm = *params.IfNoneMatch
	}
	respond.JSONWithETag(c.Writer, c.Request, http.StatusOK, inm, oapi.UserPage{
		Items:      items,
		NextCursor: page.NextCursor,
		Counts: oapi.UserCounts{
			Pending: page.Counts.Pending,
			Normal:  page.Counts.Normal,
			Vacant:  page.Counts.Vacant,
			Deleted: page.Counts.Deleted,
		},
	})
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
	if !h.reauthenticate(c, actor.UserID, body.CurrentPassword) {
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
	var body oapi.UserPasswordResetRequest
	if !decode(c, &body) {
		return
	}
	if !h.reauthenticate(c, actor.UserID, body.CurrentPassword) {
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
	var body oapi.UserDeleteRequest
	if !decode(c, &body) {
		return
	}
	if !h.reauthenticate(c, actor.UserID, body.CurrentPassword) {
		return
	}
	if err := h.svc.Delete(c.Request.Context(), scope, id, actor); err != nil {
		fail(c, err)
		return
	}
	c.Status(http.StatusNoContent)
}

// Restore 实现 POST /api/users/{id}/restore。
// 恢复会重新占用 live-user 容量并恢复登录能力，故要求二次口令。
func (h *Handler) Restore(c *gin.Context, id string, _ oapi.UserRestoreParams) {
	scope, actor, ok := h.principal(c)
	if !ok {
		return
	}
	var body oapi.UserRestoreRequest
	if !decode(c, &body) {
		return
	}
	if !h.reauthenticate(c, actor.UserID, body.CurrentPassword) {
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

// MailNotifyGet 实现 GET /api/me/mail-notify。
// 只读本人开关：用户 id 取自会话主体，请求没有可指定他人的字段。
func (h *Handler) MailNotifyGet(c *gin.Context) {
	_, actor, ok := h.principal(c)
	if !ok {
		return
	}
	enabled, err := h.svc.MailNotify(c.Request.Context(), actor.UserID)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, oapi.MailNotify{Enabled: enabled})
}

// MailNotifySet 实现 PUT /api/me/mail-notify。
// enabled 是必填布尔值，故用指针解码区分「缺字段」与 false。
func (h *Handler) MailNotifySet(c *gin.Context, _ oapi.MailNotifySetParams) {
	_, actor, ok := h.principal(c)
	if !ok {
		return
	}
	var body struct {
		Enabled *bool `json:"enabled"`
	}
	if !decode(c, &body) {
		return
	}
	if body.Enabled == nil {
		fail(c, errs.New(errs.CodeValidation, "enabled is required"))
		return
	}
	enabled, err := h.svc.SetMailNotify(c.Request.Context(), actor.UserID, *body.Enabled, actor)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, oapi.MailNotify{Enabled: enabled})
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

// userResponse 把用户行映射成契约 shape。
// 邮箱已归一；last_login_at 与 deleted_at 为 nil 时输出 null（契约要求键存在）。
func userResponse(record appuser.Record) oapi.User {
	return oapi.User{
		Id:                 record.ID,
		Email:              openapi_types.Email(record.Email),
		Role:               oapi.UserRole(record.Role),
		Status:             oapi.UserStatus(record.Status),
		MustChangePassword: record.MustChangePassword,
		AccountCount:       record.AccountCount,
		PendingCount:       record.PendingCount,
		ReviewCount:        record.ReviewCount,
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
