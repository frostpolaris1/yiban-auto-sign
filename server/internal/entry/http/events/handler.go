// Package events 把事件流与诊断用例映射到 HTTP（P6）。
//
// # 功能
// GET /api/admin/events（master/admin，SSE）、
// GET /api/me/diagnostics 与 GET /api/accounts/{id}/diagnostics
// （user/admin）、GET /api/me/sign-status（user）。
// 本包**不**写 SQL、**不**解析会话、**不**决定可见面。
//
// # 归属
// entry/http 入口层。作用域由 Authorizer（app/admin）解析；
// 诊断数据来自 app/diagnostic，事件来自 app/events.Broker。
//
// # 复用
// Handler、NewHandler；端口 Broker、Diagnostics、Authorizer、Timer。
//
// # 口径
// x-roles 是运行时门：events 限 master/admin；diagnostics 限
// user/admin；sign-status 限 user。设计取舍（snapshot 生成方）：
// snapshot 由本包在 resync 后经 Diagnostics.Snapshot 现算并直写流，
// 不经总线、不占环形缓冲——总线无库访问，发布方无需感知订阅。
// 新连接（无 Last-Event-ID）不发 snapshot：当前状态由 REST 端点提供。
package events

import (
	"context"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appdiagnostic "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/diagnostic"
	appevents "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/events"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
)

// Broker 是 SSE 处理器需要的总线子集（实现为 *appevents.Broker）。
type Broker interface {
	Subscribe(ctx context.Context, schools []string, lastEventID uint64) (appevents.Subscription, context.CancelFunc)
}

// Diagnostics 是诊断与轮询用例端口（实现为 *appdiagnostic.Service）。
type Diagnostics interface {
	Mine(ctx context.Context, userID string) (appdiagnostic.Report, error)
	Account(ctx context.Context, scope corescope.Scope, accountID string) (appdiagnostic.Detail, error)
	SignStatus(ctx context.Context, userID string) (appdiagnostic.StatusReport, error)
	Snapshot(ctx context.Context, scope corescope.Scope) (appdiagnostic.View, error)
}

// Authorizer 把会话主体解析成作用域与审计主体。
type Authorizer interface {
	Authorize(ctx context.Context, user coreauth.User) (corescope.Scope, appaudit.Actor, error)
}

// Timer 产生定时信号。生产用真计时器；测试注入假时钟驱动
// 心跳与空闲判据。为 nil 时用真计时器。
type Timer interface {
	After(d time.Duration) <-chan time.Time
}

// 心跳与空闲的缺省节奏（设计稿口径：15s 心跳、60s 空闲关闭）。
const (
	defaultHeartbeat = 15 * time.Second
	defaultIdleClose = 60 * time.Second
)

// Handler 处理事件流与诊断端点。
type Handler struct {
	broker Broker
	diag   Diagnostics
	authz  Authorizer
	clock  coreclock.Clock
	timer  Timer
}

// NewHandler 构造事件流处理器。timer 为 nil 时用真计时器。
func NewHandler(broker Broker, diag Diagnostics, authz Authorizer, clk coreclock.Clock, timer Timer) *Handler {
	return &Handler{
		broker: broker,
		diag:   diag,
		authz:  authz,
		clock:  clk,
		timer:  timer,
	}
}

// After 返回定时通道；未注入 Timer 时用真计时器。
func (h *Handler) After(d time.Duration) <-chan time.Time {
	if h.timer != nil {
		return h.timer.After(d)
	}
	return time.After(d)
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

// authorize 解析作用域；失败写错误信封并返回 false。
func (h *Handler) authorize(c *gin.Context, session coreauth.Session) (corescope.Scope, bool) {
	scope, _, err := h.authz.Authorize(c.Request.Context(), session.User)
	if err != nil {
		fail(c, err)
		return corescope.Scope{}, false
	}
	return scope, true
}

// requireKind 限定端点角色：契约 x-roles 的运行时门。
func requireKind(scope corescope.Scope, kinds ...corescope.Kind) error {
	for _, k := range kinds {
		if scope.Kind() == k {
			return nil
		}
	}
	return errs.New(errs.CodeForbidden, "role cannot access this endpoint")
}

// MeDiagnostics 实现 GET /api/me/diagnostics（user/admin）。
// 恒按本人账号：作用域在这里收敛为当前用户，admin 也只见自己的。
func (h *Handler) MeDiagnostics(c *gin.Context) {
	session, ok := h.principal(c)
	if !ok {
		return
	}
	scope, ok := h.authorize(c, session)
	if !ok {
		return
	}
	if err := requireKind(scope, corescope.KindUser, corescope.KindAdmin); err != nil {
		fail(c, err)
		return
	}
	report, err := h.diag.Mine(c.Request.Context(), session.User.ID)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, diagnosticsResponse(report))
}

// AccountDiagnostics 实现 GET /api/accounts/{id}/diagnostics（user/admin）。
// 跨用户与跨校 404 由用例的可见面判据给出。
func (h *Handler) AccountDiagnostics(c *gin.Context, id oapi.AccountID) {
	session, ok := h.principal(c)
	if !ok {
		return
	}
	scope, ok := h.authorize(c, session)
	if !ok {
		return
	}
	if err := requireKind(scope, corescope.KindUser, corescope.KindAdmin); err != nil {
		fail(c, err)
		return
	}
	detail, err := h.diag.Account(c.Request.Context(), scope, string(id))
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, accountDiagnosticsResponse(detail))
}

// MeSignStatus 实现 GET /api/me/sign-status（user）。
func (h *Handler) MeSignStatus(c *gin.Context) {
	session, ok := h.principal(c)
	if !ok {
		return
	}
	scope, ok := h.authorize(c, session)
	if !ok {
		return
	}
	if err := requireKind(scope, corescope.KindUser); err != nil {
		fail(c, err)
		return
	}
	status, err := h.diag.SignStatus(c.Request.Context(), session.User.ID)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, signStatusResponse(status))
}

// parseLastEventID 把头部值转成 uint64；缺失或非法按新连接处理。
// 非法值不报 400：续传失败的正确降级是重取全景，不是拒绝服务。
func parseLastEventID(raw *string) uint64 {
	if raw == nil {
		return 0
	}
	v := strings.TrimSpace(*raw)
	if v == "" {
		return 0
	}
	id, err := strconv.ParseUint(v, 10, 64)
	if err != nil {
		return 0
	}
	return id
}

// diagnosticsResponse 把用例报告转成传输形状。
func diagnosticsResponse(r appdiagnostic.Report) oapi.DiagnosticsReport {
	items := make([]oapi.DiagnosticsItem, len(r.Items))
	for i, it := range r.Items {
		items[i] = oapi.DiagnosticsItem{
			AccountId:   it.AccountID,
			State:       it.State,
			RunAt:       it.RunAt,
			Attempts:    it.Attempts,
			Result:      it.Result,
			NextRetryAt: it.NextRetryAt,
			Suggestion:  oapi.DiagnosticsItemSuggestion(it.Suggestion),
		}
	}
	return oapi.DiagnosticsReport{UpdatedAt: r.UpdatedAt, Items: items}
}

// accountDiagnosticsResponse 把单账号详版转成传输形状。
// 生成的 allOf 被拍平成单结构体：window 与 egress 是匿名内嵌。
func accountDiagnosticsResponse(d appdiagnostic.Detail) oapi.AccountDiagnostics {
	out := oapi.AccountDiagnostics{
		AccountId:   d.AccountID,
		State:       d.State,
		RunAt:       d.RunAt,
		Attempts:    d.Attempts,
		Result:      d.Result,
		NextRetryAt: d.NextRetryAt,
		Suggestion:  oapi.AccountDiagnosticsSuggestion(d.Suggestion),
	}
	if d.Window != nil {
		out.Window = &struct {
			End     time.Time                           `json:"end"`
			Source  oapi.AccountDiagnosticsWindowSource `json:"source"`
			Start   time.Time                           `json:"start"`
			Version int                                 `json:"version"`
		}{
			Start: d.Window.Start, End: d.Window.End,
			Source:  oapi.AccountDiagnosticsWindowSource(d.Window.Source),
			Version: d.Window.Version,
		}
	}
	if d.Egress != nil {
		out.Egress = &struct {
			Identity string `json:"identity"`
		}{Identity: d.Egress.Identity}
	}
	return out
}

// signStatusResponse 把轮询报告转成传输形状。
func signStatusResponse(s appdiagnostic.StatusReport) oapi.SignStatusReport {
	items := make([]oapi.SignStatusItem, len(s.Items))
	for i, it := range s.Items {
		items[i] = oapi.SignStatusItem{
			AccountId: it.AccountID,
			State:     it.State,
			RunAt:     it.RunAt,
			Stale:     it.Stale,
		}
	}
	return oapi.SignStatusReport{UpdatedAt: s.UpdatedAt, Items: items}
}

// fail 写标准错误信封。
func fail(c *gin.Context, err error) {
	respond.Error(c.Writer, c.Request, err)
}
