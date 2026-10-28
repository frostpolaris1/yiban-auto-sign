// Package signing 把签到凭据校验映射到 HTTP。
//
// # 功能
// POST /api/accounts/:id/verify：读取账号凭据，调用 provider.Verify。
// 本包**不**解密凭据、**不**发平台请求、**不**写库。
//
// # 归属
// entry/http 入口层。作用域由 Authorizer 解析；凭据由 app/account 提供。
//
// # 复用
// Handler、NewHandler；Service、Authorizer、Verifier 是装配接缝。
//
// # 口径
// 校验端点只允许 master 与 admin；user 一律 403。
// 手动签到允许三种角色，但账号必须在其可见面内，否则 404。
// 平台结论回 200 {ok:false, code, message}；传输失败回 502。
// 响应与消息不含手机号、口令、设备码。
package signing

import (
	"context"
	"errors"
	"net/http"

	"github.com/gin-gonic/gin"

	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/protocol"
)

// Service 是凭据读取用例端口。
type Service interface {
	SigningCredential(ctx context.Context, scope corescope.Scope, id string) (appaccount.SigningCredential, error)
}

// Authorizer 把会话主体解析成作用域。
type Authorizer interface {
	Authorize(ctx context.Context, user coreauth.User) (corescope.Scope, appaudit.Actor, error)
}

// Verifier 是签到提供方的校验接缝。
type Verifier interface {
	Verify(ctx context.Context, req contract.VerifyRequest) (contract.Profile, error)
}

// ManualSigner 是手动签到的入队接缝（实现为 app/worker.Service）。
type ManualSigner interface {
	// Request 确保当日任务行存在并返回任务 ID。
	Request(ctx context.Context, accountID string) (string, error)
}

// AccountAccess 按作用域读账号：跨校、跨用户与已软删都返回 404。
// 手动签到先用它判可见面，再入队——否则 admin 能签未绑定学校的账号。
type AccountAccess interface {
	Get(ctx context.Context, scope corescope.Scope, id string) (appaccount.Record, error)
}

// Handler 处理账号凭据校验与手动签到。
type Handler struct {
	svc      Service
	authz    Authorizer
	verifier Verifier
	manual   ManualSigner
	access   AccountAccess
}

// NewHandler 构造处理器。
func NewHandler(svc Service, authz Authorizer, verifier Verifier) *Handler {
	return &Handler{svc: svc, authz: authz, verifier: verifier}
}

// WithManual 注入手动签到接缝。未注入时该端点回 501（由路由层判定）。
func (h *Handler) WithManual(m ManualSigner) *Handler {
	h.manual = m
	return h
}

// WithAccess 注入账号可见面判据。
// **不注入属装配错误**：端点对全部角色回 500 响亮失败（fail-closed），
// 不是「user 一律拒绝」——后者会掩盖漏接线。
func (h *Handler) WithAccess(a AccountAccess) *Handler {
	h.access = a
	return h
}

// Sign 实现 POST /api/accounts/:id/sign。
//
// 成功是 202（已入队，不是已完成）。三类冲突由 worker 的错误码经
// respond 映射成 409：already_signed / sign_running / window_closed。
// 三种角色都可发起，但账号必须在其可见面内：跨校或非本人一律 404。
func (h *Handler) Sign(c *gin.Context, id oapi.AccountID, _ oapi.AccountSignParams) {
	scope, ok := h.principal(c)
	if !ok {
		return
	}
	switch scope.Kind() {
	case corescope.KindMaster, corescope.KindAdmin, corescope.KindUser:
	default:
		fail(c, errs.New(errs.CodeForbidden, "role cannot sign manually"))
		return
	}
	if h.access == nil {
		fail(c, errs.New(errs.CodeInternal, "account access is not wired"))
		return
	}
	if _, err := h.access.Get(c.Request.Context(), scope, string(id)); err != nil {
		fail(c, err)
		return
	}
	taskID, err := h.manual.Request(c.Request.Context(), string(id))
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusAccepted, oapi.AccountSignAccepted{TaskId: taskID})
}

// Verify 实现 POST /api/accounts/:id/verify。
func (h *Handler) Verify(c *gin.Context, id oapi.AccountID, _ oapi.AccountVerifyParams) {
	scope, ok := h.principal(c)
	if !ok {
		return
	}
	if kind := scope.Kind(); kind != corescope.KindMaster && kind != corescope.KindAdmin {
		fail(c, errs.New(errs.CodeForbidden, "only master or admin may verify credentials"))
		return
	}
	credential, err := h.svc.SigningCredential(c.Request.Context(), scope, string(id))
	if err != nil {
		fail(c, err)
		return
	}
	_, err = h.verifier.Verify(c.Request.Context(), contract.VerifyRequest{
		Account: contract.AccountRef{
			ID:       string(id),
			SchoolID: credential.SchoolID,
			Platform: "yiban",
		},
		Credential: contract.Credential{
			Phone:      credential.Phone,
			Password:   credential.Password,
			DeviceCode: credential.DeviceCode,
			PhoneModel: credential.PhoneModel,
		},
	})
	if err != nil {
		if result, ok := verifyConclusion(err); ok {
			respond.JSON(c.Writer, http.StatusOK, result)
			return
		}
		respond.Error(c.Writer, c.Request, errs.New(errs.CodeUpstream, "upstream unavailable"))
		return
	}
	respond.JSON(c.Writer, http.StatusOK, oapi.AccountVerifyResult{
		Ok: true, Code: string(contract.OutcomeSuccess),
	})
}

// verifyConclusion 把 provider 错误映射成中性结论。
// 平台结论与解析失败是 200；网络与未知错误返回 false，由调用方回 502 信封。
func verifyConclusion(err error) (oapi.AccountVerifyResult, bool) {
	var outcomeErr *contract.OutcomeError
	if errors.As(err, &outcomeErr) && outcomeErr != nil {
		return oapi.AccountVerifyResult{
			Ok:      false,
			Code:    string(outcomeErr.Outcome),
			Message: messagePtr(outcomeErr.Message),
		}, true
	}
	var expired *protocol.SessionExpired
	if errors.As(err, &expired) {
		return oapi.AccountVerifyResult{
			Ok: false, Code: "transient", Message: messagePtr("session expired"),
		}, true
	}
	var parse *protocol.ParseError
	if errors.As(err, &parse) {
		return oapi.AccountVerifyResult{
			Ok: false, Code: "unknown", Message: messagePtr("malformed platform response"),
		}, true
	}
	return oapi.AccountVerifyResult{}, false
}

func messagePtr(s string) *string {
	s = mask.MaskText(s)
	runes := []rune(s)
	if len(runes) > 200 {
		s = string(runes[:200])
	}
	if s == "" {
		return nil
	}
	return &s
}

func (h *Handler) principal(c *gin.Context) (corescope.Scope, bool) {
	session, ok := coreauth.FromContext(c.Request.Context())
	if !ok {
		fail(c, errs.New(errs.CodeUnauthorized, "authentication required"))
		return corescope.Scope{}, false
	}
	scope, _, err := h.authz.Authorize(c.Request.Context(), session.User)
	if err != nil {
		fail(c, err)
		return corescope.Scope{}, false
	}
	return scope, true
}

func fail(c *gin.Context, err error) {
	respond.Error(c.Writer, c.Request, err)
}
