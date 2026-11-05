// Package calendar 把用户签到日历读模型映射到 HTTP。
//
// # 功能
// GET /api/me/sign-calendar 与 GET /api/me/sign-calendar/{day}
// 两个只读端点。两者都只服务 user 角色：归属恒为会话 userID，
// 不接受任何范围或账号参数。
// 本包**不**写库、**不**聚合计数、**不**解析平台结果。
//
// # 归属
// entry/http 入口层。作用域由 Authorizer（app/admin）解析；
// 读模型由 app/calendar 提供。
//
// # 复用
// Handler、NewHandler；Service、Authorizer。
//
// # 口径
// 契约 x-roles 是 [user]：master 与 admin 回 403。
// 参数与时区校验在用例层：入口只转形状，错误走标准信封。
package calendar

import (
	"context"
	"net/http"

	"github.com/gin-gonic/gin"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
)

// Service 是日历用例端口（实现为 *appcalendar.Service）。
type Service interface {
	Range(ctx context.Context, userID, from, to string) (coreschedule.CalendarRange, error)
	Day(ctx context.Context, userID, day string) (coreschedule.CalendarDayDetail, error)
}

// Authorizer 把会话主体解析成作用域与审计主体。
type Authorizer interface {
	Authorize(ctx context.Context, user coreauth.User) (corescope.Scope, appaudit.Actor, error)
}

// Handler 处理 /api/me/sign-calendar 端点。
type Handler struct {
	svc   Service
	authz Authorizer
}

// NewHandler 构造日历处理器。
func NewHandler(svc Service, authz Authorizer) *Handler {
	return &Handler{svc: svc, authz: authz}
}

// SignCalendarGet 实现 GET /api/me/sign-calendar。
func (h *Handler) SignCalendarGet(c *gin.Context, params oapi.SignCalendarGetParams) {
	userID, ok := h.userID(c)
	if !ok {
		return
	}
	report, err := h.svc.Range(c.Request.Context(), userID, params.From, params.To)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, calendarResponse(report))
}

// SignCalendarDayGet 实现 GET /api/me/sign-calendar/{day}。
func (h *Handler) SignCalendarDayGet(c *gin.Context, day string) {
	userID, ok := h.userID(c)
	if !ok {
		return
	}
	detail, err := h.svc.Day(c.Request.Context(), userID, day)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, dayDetailResponse(detail))
}

// userID 解析会话并执行 x-roles 门：只有 user 角色可读本端点。
func (h *Handler) userID(c *gin.Context) (string, bool) {
	session, ok := coreauth.FromContext(c.Request.Context())
	if !ok {
		fail(c, errs.New(errs.CodeUnauthorized, "authentication required"))
		return "", false
	}
	scope, _, err := h.authz.Authorize(c.Request.Context(), session.User)
	if err != nil {
		fail(c, err)
		return "", false
	}
	if scope.Kind() != corescope.KindUser {
		fail(c, errs.New(errs.CodeForbidden, "role cannot access this endpoint"))
		return "", false
	}
	return session.User.ID, true
}

// calendarResponse 把读模型转成传输形状。
func calendarResponse(r coreschedule.CalendarRange) oapi.SignCalendar {
	days := make([]oapi.SignCalendarDay, len(r.Days))
	for i, day := range r.Days {
		days[i] = oapi.SignCalendarDay{
			Day:     day.Day,
			Total:   day.Total,
			Success: day.Success,
			Failed:  day.Failed,
			Skipped: day.Skipped,
			Unknown: day.Unknown,
			Future:  day.Future,
		}
	}
	return oapi.SignCalendar{
		Timezone:  r.Timezone,
		Today:     r.Today,
		UpdatedAt: r.UpdatedAt,
		Days:      days,
	}
}

// dayDetailResponse 把读模型转成传输形状。
func dayDetailResponse(d coreschedule.CalendarDayDetail) oapi.SignCalendarDayDetail {
	accounts := make([]oapi.SignCalendarAccount, len(d.Accounts))
	for i, item := range d.Accounts {
		accounts[i] = oapi.SignCalendarAccount{
			AccountId: item.AccountID,
			State:     oapi.SignCalendarAccountState(item.State),
			RunAt:     item.RunAt,
			Attempts:  item.Attempts,
		}
	}
	return oapi.SignCalendarDayDetail{
		Day:      d.Day,
		Timezone: d.Timezone,
		Accounts: accounts,
		Future:   d.Future,
	}
}

// fail 写标准错误信封。
func fail(c *gin.Context, err error) {
	respond.Error(c.Writer, c.Request, err)
}
