// Package signing 把签到凭据校验与手动签到映射到 HTTP。
//
// # 功能
// POST /api/accounts/:id/verify：读取账号凭据，调用 provider.Verify。
// POST /api/accounts/:id/sign 与 /api/accounts/batch/sign：入队手动签到。
// 本包**不**解密凭据、**不**发平台请求、**不**写库。
//
// # 归属
// entry/http 入口层。作用域由 Authorizer 解析；凭据由 app/account 提供。
//
// # 复用
// Handler、NewHandler；Service、Authorizer、Verifier 是装配接缝。
//
// # 口径
// 校验端点三种角色均可；账号必须在作用域内，越权与跨校同为 404。
// 手动签到允许三种角色，但账号必须在其可见面内，否则 404；
// 批量签到只允许 master 与 admin，user 整批 403。
// 平台结论回 200 {ok:false, code, message}；传输失败回 502。
// 成功回执可选携带平台院校两字段（院校名/院校 ID）：只在提供方
// 真的带出数据时出现。姓名属个人隐私，走 POST /api/accounts/:id/person-name
// 懒加载：成功回单字段，不落库、不进日志，审计只记动作与账号。
// 响应与消息不含手机号、口令、设备码。
package signing

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"

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
	// actor 是发起请求的会话主体：重开 failed/skipped 行要写审计。
	Request(ctx context.Context, accountID string, actor appaudit.Actor) (string, error)
}

// AccountAccess 按作用域读账号：跨校、跨用户与已软删都返回 404。
// 手动签到先用它判可见面，再入队——否则 admin 能签未绑定学校的账号。
type AccountAccess interface {
	Get(ctx context.Context, scope corescope.Scope, id string) (appaccount.Record, error)
}

// AccountLimiter 按账号键判限流（实现为 ratelimit.Guard）。
// 批量逐项接入：每个 id 在入队前判一次，与单条端点的
// accountLimit 同口径；命中项写 rate_limited 并继续下一项。
type AccountLimiter interface {
	AllowAccount(id string) (ok bool, wait time.Duration)
}

// RevealAuditor 记录一次平台姓名揭示的审计接缝（实现为 app/account.Service）。
type RevealAuditor interface {
	RecordPersonNameReveal(ctx context.Context, scope corescope.Scope, id, schoolID string, actor appaudit.Actor) error
}

// Handler 处理账号凭据校验与手动签到。
type Handler struct {
	svc      Service
	authz    Authorizer
	verifier Verifier
	manual   ManualSigner
	access   AccountAccess
	limiter  AccountLimiter
	reveals  RevealAuditor
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

// WithRevealAudit 注入姓名揭示的审计接缝。
// **不注入属装配错误**：姓名揭示必须留痕，缺口时 PersonName 端点
// 回 500 响亮失败（fail-closed），不是「静默不给姓名」。
func (h *Handler) WithRevealAudit(a RevealAuditor) *Handler {
	h.reveals = a
	return h
}

// WithAccountLimiter 注入账号键限流（批量签到用）。
// **不注入属装配错误**：批量端点回 500 响亮失败——契约声明的 429
// 若运行时不可达，就是限流面静默缺失。
func (h *Handler) WithAccountLimiter(l AccountLimiter) *Handler {
	h.limiter = l
	return h
}

// Sign 实现 POST /api/accounts/:id/sign。
//
// 成功是 202（已入队，不是已完成）。冲突由 worker 的错误码经
// respond 映射成 409：already_signed / sign_running / window_closed；
// 多任务是同一打卡的多个点位，由 worker 随机选一个，不再报冲突。
// 非 active 账号另由本层回 409 conflict。三种角色都可发起，
// 但账号必须在其可见面内：跨校或非本人一律 404。
func (h *Handler) Sign(c *gin.Context, id oapi.AccountID, _ oapi.AccountSignParams) {
	scope, actor, ok := h.principal(c)
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
	record, err := h.access.Get(c.Request.Context(), scope, string(id))
	if err != nil {
		fail(c, err)
		return
	}
	// 旧线签到按钮只对 active 出现（web/routes/signin_api.py 对 status != active
	// 一律拒绝）。这里把非 active 从“找不到可用任务”的 404 收敛成 409 conflict：
	// “不入队”的根守卫仍在 resolveTask 的 active 谓词与 worker 认领查询。
	if !record.Status.Active() {
		fail(c, errs.New(errs.CodeConflict, "account is not active"))
		return
	}
	taskID, err := h.manual.Request(c.Request.Context(), string(id), actor)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusAccepted, oapi.AccountSignAccepted{TaskId: taskID})
}

// 批量项状态词表：契约枚举 AccountBatchSignItemStatus 的运行时同集。
// 门在 oapi 包 signing_batch_status_gate_test.go，任一侧增删改值即红。
const (
	BatchSignItemStatusOK    = "ok"
	BatchSignItemStatusError = "error"
)

// batchSignLimit 是一次批量签到的账号数上限。
const batchSignLimit = 10

// BatchSign 实现 POST /api/accounts/batch/sign。
//
// master 与 admin 可用，user 整批 403。逐条走单条签到同一条路：
// 账号键限流（与单条端点同口径，命中项 per-item rate_limited）→
// 可见面（跨校/非本人 per-item not_found）→ 非 active 拒入队（per-item
// conflict）→ 入队（worker 稳定码 already_signed / sign_running /
// window_closed）。多任务由 worker 随机选一个，不再报冲突。
// 一条失败只写进该项，不中断批次；
// 成功项只入队（202 语义），task_id 只在成功项出现，顺序与请求 ids 一致。
// 客户端 IP 面由路由中间件对整批限流（rateLimitedPaths 白名单）。
//
// 与旧线口径的偏差：没有进程级 60 秒冷却，也没有宿主级单队列——
// 旧 Python 服务串行化在一个进程里；新线的并发语义归 IP/账号键限流、
// worker 容量与账号级唯一任务行，ids 去重保证一批内同一账号只入队一次。
// 不要求二次口令，与单条签到同口径。
func (h *Handler) BatchSign(c *gin.Context, _ oapi.AccountBatchSignParams) {
	scope, actor, ok := h.principal(c)
	if !ok {
		return
	}
	if kind := scope.Kind(); kind != corescope.KindMaster && kind != corescope.KindAdmin {
		fail(c, errs.New(errs.CodeForbidden, "only master or admin may batch sign"))
		return
	}
	// 可见面判据或限流器未接线＝装配错误，响亮失败（与 Sign 同门）。
	if h.access == nil {
		fail(c, errs.New(errs.CodeInternal, "account access is not wired"))
		return
	}
	if h.manual == nil {
		fail(c, errs.New(errs.CodeInternal, "manual signer is not wired"))
		return
	}
	if h.limiter == nil {
		fail(c, errs.New(errs.CodeInternal, "account limiter is not wired"))
		return
	}
	var body oapi.AccountBatchSignRequest
	if !decode(c, &body) {
		return
	}
	if err := validateBatchIDs(body.Ids); err != nil {
		fail(c, err)
		return
	}
	items := make([]oapi.AccountBatchSignItem, 0, len(body.Ids))
	for _, id := range body.Ids {
		items = append(items, h.batchSignOne(c.Request.Context(), scope, actor, id))
	}
	respond.JSON(c.Writer, http.StatusOK, oapi.AccountBatchSignResult{Items: items})
}

// batchSignOne 处理单个 id：错误写进该项并继续，不中断批次。
func (h *Handler) batchSignOne(ctx context.Context, scope corescope.Scope, actor appaudit.Actor, id string) oapi.AccountBatchSignItem {
	item := oapi.AccountBatchSignItem{Id: id}
	// 账号键限流先于一切判定，与单条端点同序：router 对单条在进
	// handler 前判 accountLimit(id)，键先小写去首尾空白——UUID 大小写
	// 不敏感，不归一就能用变体拆桶绕过。只归一限流键，Get/Request
	// 仍收原样 id。命中只拒本项，批次继续。
	if ok, _ := h.limiter.AllowAccount(strings.ToLower(strings.TrimSpace(id))); !ok {
		return batchItemError(item, errs.New(errs.CodeRateLimited, "too many sign requests for this account"))
	}
	record, err := h.access.Get(ctx, scope, id)
	if err != nil {
		return batchItemError(item, err)
	}
	// 非 active 与单条同口径：conflict，不入队。
	if !record.Status.Active() {
		return batchItemError(item, errs.New(errs.CodeConflict, "account is not active"))
	}
	taskID, err := h.manual.Request(ctx, id, actor)
	if err != nil {
		return batchItemError(item, err)
	}
	item.Status = oapi.AccountBatchSignItemStatus(BatchSignItemStatusOK)
	item.TaskId = &taskID
	return item
}

// batchItemError 把稳定码写进错误项。
func batchItemError(item oapi.AccountBatchSignItem, err error) oapi.AccountBatchSignItem {
	e := errs.From(err)
	item.Status = oapi.AccountBatchSignItemStatus(BatchSignItemStatusError)
	code := oapi.ErrorCode(e.Code)
	item.Code = &code
	item.Message = &e.Message
	return item
}

// validateBatchIDs 钉住整批口径：空、超限、重复一律 400，一条都不跑。
func validateBatchIDs(ids []string) error {
	switch {
	case len(ids) == 0:
		return errs.New(errs.CodeValidation, "ids must not be empty")
	case len(ids) > batchSignLimit:
		return errs.New(errs.CodeValidation, "ids must hold at most 10 entries")
	}
	seen := make(map[string]struct{}, len(ids))
	for _, id := range ids {
		if _, dup := seen[id]; dup {
			return errs.New(errs.CodeValidation, "ids must not repeat")
		}
		seen[id] = struct{}{}
	}
	return nil
}

// decode 解 JSON 请求体；绑定失败回 400 信封。
func decode(c *gin.Context, target any) bool {
	if err := json.NewDecoder(c.Request.Body).Decode(target); err != nil {
		respond.BindError(c.Writer, c.Request, err)
		return false
	}
	return true
}

// Verify 实现 POST /api/accounts/:id/verify。
// 三种角色都可校验；作用域由 SigningCredential 判定，
// user 只能读自己的账号，越权与跨校一样 404（person-name 同口径）。
func (h *Handler) Verify(c *gin.Context, id oapi.AccountID, _ oapi.AccountVerifyParams) {
	scope, _, ok := h.principal(c)
	if !ok {
		return
	}
	credential, err := h.svc.SigningCredential(c.Request.Context(), scope, string(id))
	if err != nil {
		fail(c, err)
		return
	}
	profile, err := h.verifier.Verify(c.Request.Context(), contract.VerifyRequest{
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
	respond.JSON(c.Writer, http.StatusOK, verifyResultFromProfile(profile))
}

// verifyResultFromProfile 把成功校验的回执映射成响应。
// 平台可能只回签发回执不带身份数据（data={}）：identity 院校两字段
// 不全时整键缺席，不回一个空对象假装有数据。
// 姓名属个人隐私，不在此处透出：走 POST /api/accounts/{id}/person-name 懒加载。
func verifyResultFromProfile(profile contract.Profile) oapi.AccountVerifyResult {
	result := oapi.AccountVerifyResult{Ok: true, Code: string(contract.OutcomeSuccess)}
	if profile.SchoolName != "" && profile.UniversityID != "" {
		result.Identity = &oapi.AccountVerifyIdentity{
			UniversityName: profile.SchoolName,
			UniversityId:   profile.UniversityID,
		}
	}
	return result
}

// PersonName 实现 POST /api/accounts/:id/person-name。
// 懒加载与隐私门：与 verify 同型的平台认证，成功只回姓名单字段。
// 姓名不落库、不进日志正文；审计行只记动作与账号（detail 是字段名非值）。
// 作用域判定在 SigningCredential：user 越权他人与跨校一样 404。
// 平台结论与传输失败统一 502 中性信封（device-refresh 先例）：
// 姓名响应是单字段，没有结论形状可回。
func (h *Handler) PersonName(c *gin.Context, id oapi.AccountID, _ oapi.AccountPersonNameParams) {
	scope, actor, ok := h.principal(c)
	if !ok {
		return
	}
	if h.reveals == nil {
		fail(c, errs.New(errs.CodeInternal, "reveal auditor is not wired"))
		return
	}
	credential, err := h.svc.SigningCredential(c.Request.Context(), scope, string(id))
	if err != nil {
		fail(c, err)
		return
	}
	profile, err := h.verifier.Verify(c.Request.Context(), contract.VerifyRequest{
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
		respond.Error(c.Writer, c.Request, errs.New(errs.CodeUpstream, "person name lookup failed"))
		return
	}
	// 审计先于响应：追加失败就 5xx，姓名不出本函数。
	if err := h.reveals.RecordPersonNameReveal(c.Request.Context(), scope, string(id), credential.SchoolID, actor); err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, oapi.AccountPersonNameResult{PersonName: profile.DisplayName})
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

// principal 返回作用域与审计主体。审计主体供手动签到的重开审计使用。
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

func fail(c *gin.Context, err error) {
	respond.Error(c.Writer, c.Request, err)
}
