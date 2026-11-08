// Package account 把账号用例映射到 HTTP。
//
// # 功能
// 列表、创建、读取、汇总、修改、审核、暂停、软删、恢复、设备刷新、
// 批量动作十一个端点。
// 本包**不**写 SQL、**不**判状态迁移、**不**加密凭据。
//
// # 归属
// entry/http 入口层。作用域由 Authorizer（app/schooladmin）解析。
//
// # 复用
// Handler、NewHandler；Service、Authorizer 是装配接缝。
//
// # 口径
// 响应只给 phone_masked 与 owner_email_masked；原始手机号与邮箱在 Record 里，
// **不出本层**。口令与设备码只进请求体，本层不记录、不回显。
// 凭据编辑（phone/password/device_code）、删号、自助删号、设备刷新与
// 恢复（管理员恢复与自助恢复）是危险操作：先验二次口令（acting user
// 自己的登录口令）再进用例，失败零写入。只改 name/phone_model/school_id
// 不要求二次口令。
// phone_model 来自凭据密文，只回机型；设备码与口令永不出服务端。
package account

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"math"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/auth"
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
	Summary(ctx context.Context, scope corescope.Scope) (appaccount.StatusCounts, error)
	Update(ctx context.Context, scope corescope.Scope, id string, in appaccount.UpdateInput, actor appaudit.Actor) (appaccount.Record, error)
	Review(ctx context.Context, scope corescope.Scope, id string, approve bool, reason string, actor appaudit.Actor) (appaccount.Record, error)
	Pause(ctx context.Context, scope corescope.Scope, id string, paused bool, actor appaudit.Actor) (appaccount.Record, error)
	Delete(ctx context.Context, scope corescope.Scope, id string, actor appaudit.Actor) error
	Restore(ctx context.Context, scope corescope.Scope, id string, actor appaudit.Actor) (appaccount.Record, error)
	// Purge 物理删除一个已软删账号；只允许 master 与 admin。
	Purge(ctx context.Context, scope corescope.Scope, id string, actor appaudit.Actor) error
	// Batch 对 1–10 个账号执行同一动作，逐条独立事务、逐条审计；
	// 请求级校验失败返回错误，单条失败写入该条结果并继续。
	Batch(ctx context.Context, scope corescope.Scope, action string, ids []string, reason string, actor appaudit.Actor) ([]appaccount.BatchItemResult, error)
	// SelfDelete 与 SelfRestore 只作用于调用者自己的账号；
	// scope 由入口层用会话 user id 构造，不来自请求字段。
	SelfDelete(ctx context.Context, scope corescope.Scope, id string, actor appaudit.Actor) error
	SelfRestore(ctx context.Context, scope corescope.Scope, id string, actor appaudit.Actor) (appaccount.Record, error)
	DeviceRefresh(ctx context.Context, scope corescope.Scope, id string, actor appaudit.Actor) (appaccount.DeviceRefreshResult, error)
	// Detect 用提交的手机号与口令做一次平台探测；凭据不落库。
	Detect(ctx context.Context, scope corescope.Scope, in appaccount.DetectInput) (appaccount.DetectResult, error)
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

// Throttle 是账号变更的每属主限流面（实现为 ratelimit.Guard）。
// nil 表示不限流：只用于测试与降级进程。
type Throttle interface {
	AllowAccount(key string) (bool, time.Duration)
}

// Handler 处理 /api/accounts 下的全部端点。
type Handler struct {
	svc     Service
	authz   Authorizer
	limiter Throttle
	reauth  Reauth
}

// NewHandler 构造账号处理器。limiter 可省略（测试与降级进程）。
func NewHandler(svc Service, authz Authorizer, limiter ...Throttle) *Handler {
	var l Throttle
	if len(limiter) > 0 {
		l = limiter[0]
	}
	return &Handler{svc: svc, authz: authz, limiter: l}
}

// WithReauth 注入危险操作二次口令用例。
// 未注入时凭据编辑、删号、自助删号、设备刷新与恢复按 internal 拒绝（失败关闭）。
func (h *Handler) WithReauth(r Reauth) *Handler {
	h.reauth = r
	return h
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
	if params.IncludeDeleted != nil {
		filter.IncludeDeleted = *params.IncludeDeleted
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
	// 列表可变：ETag 只省响应体，鉴权与作用域判定已经完成。
	inm := ""
	if params.IfNoneMatch != nil {
		inm = *params.IfNoneMatch
	}
	respond.JSONWithETag(c.Writer, c.Request, http.StatusOK, inm,
		oapi.AccountPage{Items: items, NextCursor: page.NextCursor})
}

// Create 实现 POST /api/accounts。
func (h *Handler) Create(c *gin.Context, _ oapi.AccountCreateParams) {
	scope, actor, ok := h.principal(c)
	if !ok {
		return
	}
	if h.accountRateLimited(c, actor.UserID) {
		return
	}
	var body oapi.AccountCreate
	if !decode(c, &body) {
		return
	}
	in := appaccount.CreateInput{
		SchoolID:   body.SchoolId,
		Phone:      body.Phone,
		Name:       stringOf(body.Name),
		Password:   body.Password,
		DeviceCode: stringOf(body.DeviceCode),
		PhoneModel: stringOf(body.PhoneModel),
	}
	if body.ConfirmDefaults != nil {
		in.ConfirmDefaults = *body.ConfirmDefaults
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

// Detect 实现 POST /api/accounts/detect。
// 凭据只进用例；本层不记录、不回显。限流键用手机号摘要：
// 桶里不出现明文号码，同一号码的变体仍落同一键。
func (h *Handler) Detect(c *gin.Context) {
	scope, _, ok := h.principal(c)
	if !ok {
		return
	}
	var body oapi.AccountDetectRequest
	if !decode(c, &body) {
		return
	}
	if h.detectRateLimited(c, body.Phone) {
		return
	}
	result, err := h.svc.Detect(c.Request.Context(), scope, appaccount.DetectInput{
		Phone: body.Phone, Password: body.Password,
	})
	if err != nil {
		fail(c, err)
		return
	}
	out := oapi.AccountDetectResult{
		UniversityName:    result.UniversityName,
		UniversityId:      result.UniversityID,
		PersonName:        result.PersonName,
		DeviceModel:       result.DeviceModel,
		DeviceCodePresent: result.DeviceCodePresent,
	}
	if result.School != nil {
		out.School = &struct {
			Id   string `json:"id"`
			Name string `json:"name"`
		}{Id: result.School.ID, Name: result.School.Name}
	}
	respond.JSON(c.Writer, http.StatusOK, out)
}

// detectRateLimited 按提交手机号的摘要限制探测频率。
// 与账号/签到桶分开：detect 发生在账号存在之前，键面是平台手机号。
func (h *Handler) detectRateLimited(c *gin.Context, phone string) bool {
	if h.limiter == nil {
		return false
	}
	ok, wait := h.limiter.AllowAccount(detectRateKey(phone))
	if ok {
		return false
	}
	seconds := int(math.Ceil(wait.Seconds()))
	if seconds < 1 {
		seconds = 1
	}
	c.Header("Retry-After", strconv.Itoa(seconds))
	fail(c, errs.New(errs.CodeRateLimited, "too many platform detection requests"))
	return true
}

// detectRateKey 返回不含明文手机号的限流键。
func detectRateKey(phone string) string {
	sum := sha256.Sum256([]byte("detect:" + strings.TrimSpace(phone)))
	return "detect:" + hex.EncodeToString(sum[:16])
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

// Summary 实现 GET /api/accounts/summary（静态段，紧邻 {id} 路由）。
// 只读计数：角色门槛（master/admin）在用例层，本层不重判。
func (h *Handler) Summary(c *gin.Context) {
	scope, _, ok := h.principal(c)
	if !ok {
		return
	}
	counts, err := h.svc.Summary(c.Request.Context(), scope)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, oapi.AccountSummary{
		Pending:  counts.Pending,
		Rejected: counts.Rejected,
	})
}

// Update 实现 PATCH /api/accounts/{id}。
// body 带 phone/password/device_code（凭据面）时要求二次口令；
// 只改 name/phone_model/school_id 不要求。
func (h *Handler) Update(c *gin.Context, id oapi.AccountID, _ oapi.AccountUpdateParams) {
	scope, actor, ok := h.principal(c)
	if !ok {
		return
	}
	if h.accountRateLimited(c, actor.UserID) {
		return
	}
	var body oapi.AccountUpdate
	if !decode(c, &body) {
		return
	}
	if touchesCredential(body) {
		if !h.reauthenticate(c, actor.UserID, body.CurrentPassword) {
			return
		}
	}
	record, err := h.svc.Update(c.Request.Context(), scope, id, appaccount.UpdateInput{
		SchoolID:          body.SchoolId,
		Phone:             body.Phone,
		Name:              body.Name,
		Password:          body.Password,
		DeviceCode:        body.DeviceCode,
		PhoneModel:        body.PhoneModel,
		ExpectedUpdatedAt: body.ExpectedUpdatedAt,
	}, actor)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, accountResponse(record))
}

// touchesCredential 判改号请求是否携带凭据面字段。
// 键存在即算：device_code 空串对 master/admin 是清除动作，同样是凭据面。
func touchesCredential(body oapi.AccountUpdate) bool {
	return body.Phone != nil || body.Password != nil || body.DeviceCode != nil
}

// Delete 实现 DELETE /api/accounts/{id}。
// 总是要求二次口令；请求体现在契约里带 current_password。
func (h *Handler) Delete(c *gin.Context, id oapi.AccountID, _ oapi.AccountDeleteParams) {
	scope, actor, ok := h.principal(c)
	if !ok {
		return
	}
	var body oapi.AccountDeleteRequest
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

// Restore 实现 POST /api/accounts/{id}/restore。
// 角色门槛（master/admin）与容量判定都在用例与适配器，本层不重判。
// 恢复会把账号重新计入容量并让它再次参与签到，故要求二次口令。
func (h *Handler) Restore(c *gin.Context, id oapi.AccountID, _ oapi.AccountRestoreParams) {
	scope, actor, ok := h.principal(c)
	if !ok {
		return
	}
	var body oapi.AccountRestoreRequest
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
	respond.JSON(c.Writer, http.StatusOK, accountResponse(record))
}

// Purge 实现 POST /api/accounts/{id}/purge。
// 物理清除不可撤销，故与恢复同门槛：先验二次口令再进用例。
// 角色门槛（master/admin）与软删状态判定都在用例与适配器，本层不重判。
func (h *Handler) Purge(c *gin.Context, id oapi.AccountID, _ oapi.AccountPurgeParams) {
	scope, actor, ok := h.principal(c)
	if !ok {
		return
	}
	var body oapi.AccountPurgeRequest
	if !decode(c, &body) {
		return
	}
	if !h.reauthenticate(c, actor.UserID, body.CurrentPassword) {
		return
	}
	if err := h.svc.Purge(c.Request.Context(), scope, id, actor); err != nil {
		fail(c, err)
		return
	}
	c.Status(http.StatusNoContent)
}

// MyAccountDelete 实现 POST /api/me/accounts/{id}/delete。
// 自助面只认会话 user id：任何角色都只能删自己名下的行。
// 总是要求二次口令。
// 他人账号由仓储的范围谓词报 404，本层不先读、不暴露存在性。
func (h *Handler) MyAccountDelete(c *gin.Context, id oapi.AccountID, _ oapi.MyAccountDeleteParams) {
	scope, actor, ok := h.selfPrincipal(c)
	if !ok {
		return
	}
	var body oapi.MyAccountDeleteRequest
	if !decode(c, &body) {
		return
	}
	if !h.reauthenticate(c, actor.UserID, body.CurrentPassword) {
		return
	}
	if err := h.svc.SelfDelete(c.Request.Context(), scope, id, actor); err != nil {
		fail(c, err)
		return
	}
	c.Status(http.StatusNoContent)
}

// MyAccountRestore 实现 POST /api/me/accounts/{id}/restore。
// 管理员删除的账号、超窗与已有生效账号的判据都在用例与适配器。
// 恢复会把账号重新计入容量并让它再次参与签到，故要求二次口令。
func (h *Handler) MyAccountRestore(c *gin.Context, id oapi.AccountID, _ oapi.MyAccountRestoreParams) {
	scope, actor, ok := h.selfPrincipal(c)
	if !ok {
		return
	}
	var body oapi.MyAccountRestoreRequest
	if !decode(c, &body) {
		return
	}
	if !h.reauthenticate(c, actor.UserID, body.CurrentPassword) {
		return
	}
	record, err := h.svc.SelfRestore(c.Request.Context(), scope, id, actor)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, accountResponse(record))
}

// DeviceRefresh 实现 POST /api/accounts/{id}/device-refresh。
// 设备身份即凭据：总是要求二次口令。
// 用例返回的 DeviceCode 是明文；在这里掩码后进响应，
// **本函数与响应之间不再有明文出口**（掩码即豁免边界）。
func (h *Handler) DeviceRefresh(c *gin.Context, id oapi.AccountID, _ oapi.AccountDeviceRefreshParams) {
	scope, actor, ok := h.principal(c)
	if !ok {
		return
	}
	var body oapi.AccountDeviceRefreshRequest
	if !decode(c, &body) {
		return
	}
	if !h.reauthenticate(c, actor.UserID, body.CurrentPassword) {
		return
	}
	result, err := h.svc.DeviceRefresh(c.Request.Context(), scope, id, actor)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, oapi.AccountDevice{
		DeviceCodeMasked: mask.MaskDevice(result.DeviceCode),
		PhoneModel:       result.PhoneModel,
		Authorized:       result.Authorized,
	})
}

// Batch 实现 POST /api/accounts/batch。
// 角色门槛（master/admin）与逐条作用域判定都在用例层，本层不重判。
// 二次口令与单条危险操作同形：缺失 400、错误 403、连续失败 429；
// 失败零写入——用例不被调用，任何一条都不落库不审计。
// 响应 items 与请求 ids 同序；单条失败不阻断后续。
func (h *Handler) Batch(c *gin.Context, _ oapi.AccountBatchParams) {
	scope, actor, ok := h.principal(c)
	if !ok {
		return
	}
	var body oapi.AccountBatchRequest
	if !decode(c, &body) {
		return
	}
	if !h.reauthenticate(c, actor.UserID, &body.CurrentPassword) {
		return
	}
	results, err := h.svc.Batch(c.Request.Context(), scope,
		string(body.Action), body.Ids, stringOf(body.Reason), actor)
	if err != nil {
		fail(c, err)
		return
	}
	items := make([]oapi.AccountBatchItem, 0, len(results))
	for _, result := range results {
		item := oapi.AccountBatchItem{Id: result.ID}
		if result.OK() {
			item.Status = oapi.AccountBatchItemStatusOk
		} else {
			item.Status = oapi.AccountBatchItemStatusError
			code := oapi.ErrorCode(result.Code)
			message := result.Message
			item.Code = &code
			item.Message = &message
		}
		items = append(items, item)
	}
	respond.JSON(c.Writer, http.StatusOK, oapi.AccountBatchResult{Items: items})
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

// selfPrincipal 构造只覆盖会话用户自己的作用域。
// 角色不改变作用域形状：master/admin 的自助动作同样只碰自己的行。
func (h *Handler) selfPrincipal(c *gin.Context) (corescope.Scope, appaudit.Actor, bool) {
	session, ok := coreauth.FromContext(c.Request.Context())
	if !ok {
		fail(c, errs.New(errs.CodeUnauthorized, "authentication required"))
		return corescope.Scope{}, appaudit.Actor{}, false
	}
	_, actor, err := h.authz.Authorize(c.Request.Context(), session.User)
	if err != nil {
		fail(c, err)
		return corescope.Scope{}, appaudit.Actor{}, false
	}
	return corescope.User(session.User.ID), actor, true
}

func accountResponse(record appaccount.Record) oapi.Account {
	out := oapi.Account{
		Id:               record.ID,
		SchoolId:         record.SchoolID,
		OwnerUserId:      record.OwnerUserID,
		OwnerEmailMasked: mask.EmailLabel(record.OwnerEmail),
		PhoneMasked:      mask.MaskPhone(record.Phone),
		Name:             record.Name,
		DisplayName:      record.DisplayName,
		Status:           oapi.AccountStatus(record.Status),
		// 拒绝理由是自由文本；写库时保留原文，**出口一律掩码**。
		// 审核、读取、列表三条响应同源，故只在这里处理一次。
		RejectReason: mask.MaskText(record.RejectReason),
		ReviewedAt:   record.ReviewedAt,
		CreatedAt:    record.CreatedAt,
		UpdatedAt:    record.UpdatedAt,
		DeletedAt:    record.DeletedAt,
	}
	if record.ReviewedBy != "" {
		reviewedBy := record.ReviewedBy
		out.ReviewedBy = &reviewedBy
	}
	// 机型来自凭据明文；空串表示未设置或不可读，此时不发该字段。
	if record.PhoneModel != "" {
		phoneModel := record.PhoneModel
		out.PhoneModel = &phoneModel
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

// accountRateLimited 按属主限制建号与改号的频率。
// 拒绝时写 Retry-After 与 429；用例不再被调用。
func (h *Handler) accountRateLimited(c *gin.Context, userID string) bool {
	if h.limiter == nil {
		return false
	}
	ok, wait := h.limiter.AllowAccount("account-actions:" + userID)
	if ok {
		return false
	}
	seconds := int(math.Ceil(wait.Seconds()))
	if seconds < 1 {
		seconds = 1
	}
	c.Header("Retry-After", strconv.Itoa(seconds))
	fail(c, errs.New(errs.CodeRateLimited, "too many account changes"))
	return true
}

func fail(c *gin.Context, err error) {
	respond.Error(c.Writer, c.Request, err)
}
