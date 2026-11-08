// Package system 把设置名册、全站公告与测试邮件映射到 HTTP。
//
// 功能：
// GET /api/admin/settings（master/admin 只读名册）、
// POST /api/admin/mail/test（master 向自己的邮箱发测试信）、
// PUT /api/admin/announcement（master）、GET /api/announcement（所有已登录角色）。
// 本包**不**写 SQL、**不**回显敏感值。
//
// 归属：entry/http 入口层。用例来自 app/settings；
// 角色是运行时门：设置名册限 master/admin，公告写与测试邮件限 master。
//
// 口径：敏感配置只回 {set, masked, source}；公告正文不经审计 detail。
// 测试邮件收件人只取会话用户邮箱，响应只给固定文案。
package system

import (
	"context"
	"encoding/json"
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appsettings "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/settings"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
)

// Roster 是只读设置名册端口。
type Roster interface {
	Report() appsettings.Report
}

// Announcement 是公告用例端口。
type Announcement interface {
	Get(ctx context.Context) (appsettings.Announcement, error)
	Set(ctx context.Context, text string, actor appaudit.Actor) (appsettings.Announcement, error)
}

// MailTest 是测试邮件用例端口。
type MailTest interface {
	SendTestMail(ctx context.Context, user coreauth.User) error
}

// AutoApprove 是全局自动审批开关端口。
type AutoApprove interface {
	AutoApprove(ctx context.Context, scope corescope.Scope) (bool, error)
	SetAutoApprove(ctx context.Context, scope corescope.Scope, actor appaudit.Actor, enabled bool) (bool, error)
}

// Handler 处理设置名册、公告、测试邮件与自动审批开关端点。
type Handler struct {
	roster       Roster
	announcement Announcement
	mailTest     MailTest
	autoApprove  AutoApprove
}

// NewHandler 构造系统设置处理器。
func NewHandler(roster Roster, announcement Announcement, mailTest MailTest) *Handler {
	return &Handler{roster: roster, announcement: announcement, mailTest: mailTest}
}

// WithAutoApprove 注入自动审批开关用例。
// 未注入时两个端点回 501：开关面缺失必须显性，不得读成关闭。
func (h *Handler) WithAutoApprove(auto AutoApprove) *Handler {
	h.autoApprove = auto
	return h
}

// AdminSettings 实现 GET /api/admin/settings。
func (h *Handler) AdminSettings(c *gin.Context) {
	session, ok := requester(c)
	if !ok {
		return
	}
	if !h.allowAdmin(c, session) {
		return
	}
	if h.roster == nil {
		respond.NotImplemented(c.Writer, c.Request)
		return
	}
	report := h.roster.Report()
	groups := make([]oapi.SettingGroup, 0, len(report.Groups))
	for _, group := range report.Groups {
		items := make([]oapi.SettingItem, 0, len(group.Items))
		for _, item := range group.Items {
			items = append(items, settingResponse(item))
		}
		groups = append(groups, oapi.SettingGroup{Name: group.Name, Items: items})
	}
	respond.JSON(c.Writer, http.StatusOK, oapi.SettingsReport{Groups: groups})
}

// AdminMailTest 实现 POST /api/admin/mail/test。仅 master。
// 请求体为空且被忽略：收件人只取会话用户邮箱，客户端无法指定。
func (h *Handler) AdminMailTest(c *gin.Context, _ oapi.AdminMailTestParams) {
	session, ok := requester(c)
	if !ok {
		return
	}
	if session.User.Role != coreauth.RoleMaster {
		fail(c, errs.New(errs.CodeForbidden, "master role is required"))
		return
	}
	if h.mailTest == nil {
		respond.NotImplemented(c.Writer, c.Request)
		return
	}
	if err := h.mailTest.SendTestMail(c.Request.Context(), session.User); err != nil {
		retryAfter(c, err)
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, oapi.MailTestResult{
		Message: "test email accepted by the SMTP server",
	})
}

// retryAfter 把冷却等待写进 Retry-After 头。
// 只有用例层显式带 retry_after_seconds 时才写；其余 429 保持无头。
func retryAfter(c *gin.Context, err error) {
	e := errs.From(err)
	if e == nil || e.Code != errs.CodeRateLimited {
		return
	}
	seconds, ok := e.Details["retry_after_seconds"].(int)
	if !ok || seconds < 1 {
		seconds = int(appsettings.TestMailCooldown / time.Second)
	}
	if seconds < 1 {
		seconds = 1
	}
	c.Header("Retry-After", strconv.Itoa(seconds))
}

// AdminAutoApprove 实现 GET /api/admin/auto-approve。
// master 与 admin 可读；开关值来自 settings 表，缺行按关闭。
func (h *Handler) AdminAutoApprove(c *gin.Context) {
	session, ok := requester(c)
	if !ok {
		return
	}
	if !h.allowAdmin(c, session) {
		return
	}
	if h.autoApprove == nil {
		respond.NotImplemented(c.Writer, c.Request)
		return
	}
	enabled, err := h.autoApprove.AutoApprove(c.Request.Context(), scopeOf(session.User))
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, oapi.AutoApproveStatus{Enabled: enabled})
}

// AdminAutoApproveSet 实现 PUT /api/admin/auto-approve。仅 master。
// enabled 是必填布尔：缺字段与 null 都回 400，不能被零值读成「关闭」。
func (h *Handler) AdminAutoApproveSet(c *gin.Context, _ oapi.AdminAutoApproveSetParams) {
	session, ok := requester(c)
	if !ok {
		return
	}
	if session.User.Role != coreauth.RoleMaster {
		fail(c, errs.New(errs.CodeForbidden, "master role is required"))
		return
	}
	if h.autoApprove == nil {
		respond.NotImplemented(c.Writer, c.Request)
		return
	}
	var body struct {
		Enabled *bool `json:"enabled"`
	}
	if err := json.NewDecoder(c.Request.Body).Decode(&body); err != nil {
		respond.BindError(c.Writer, c.Request, err)
		return
	}
	if body.Enabled == nil {
		fail(c, errs.New(errs.CodeValidation, "enabled is required"))
		return
	}
	enabled, err := h.autoApprove.SetAutoApprove(c.Request.Context(), scopeOf(session.User),
		appaudit.NewActor(session.User), *body.Enabled)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, oapi.AutoApproveStatus{Enabled: enabled})
}

// scopeOf 由会话角色构造设置读写所需的窄作用域。
// 自动审批开关不按院校分片；admin 用空绑定集，数据面不会泄漏任何学校。
func scopeOf(user coreauth.User) corescope.Scope {
	switch user.Role {
	case coreauth.RoleMaster:
		return corescope.Master()
	case coreauth.RoleAdmin:
		return corescope.Admin(nil)
	default:
		return corescope.User(user.ID)
	}
}

// AnnouncementGet 实现 GET /api/announcement。所有已登录角色可读。
func (h *Handler) AnnouncementGet(c *gin.Context) {
	if _, ok := requester(c); !ok {
		return
	}
	announcement, err := h.announcement.Get(c.Request.Context())
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, announcementResponse(announcement))
}

// AnnouncementSet 实现 PUT /api/admin/announcement。仅 master。
func (h *Handler) AnnouncementSet(c *gin.Context, _ oapi.AnnouncementSetParams) {
	session, ok := requester(c)
	if !ok {
		return
	}
	if session.User.Role != coreauth.RoleMaster {
		fail(c, errs.New(errs.CodeForbidden, "master role is required"))
		return
	}
	// text 是必填键：缺失不得等同于「清空公告」。
	var body struct {
		Text *string `json:"text"`
	}
	if err := json.NewDecoder(c.Request.Body).Decode(&body); err != nil {
		respond.BindError(c.Writer, c.Request, err)
		return
	}
	if body.Text == nil {
		fail(c, errs.New(errs.CodeValidation, "text is required"))
		return
	}
	announcement, err := h.announcement.Set(c.Request.Context(), *body.Text, appaudit.NewActor(session.User))
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, announcementResponse(announcement))
}

// allowAdmin 是 master/admin 角色门。user 一律 403。
func (h *Handler) allowAdmin(c *gin.Context, session coreauth.Session) bool {
	if session.User.Role == coreauth.RoleMaster || session.User.Role == coreauth.RoleAdmin {
		return true
	}
	fail(c, errs.New(errs.CodeForbidden, "master or admin role is required"))
	return false
}

// requester 从请求上下文取会话；未登录或非活跃主体 401。
func requester(c *gin.Context) (coreauth.Session, bool) {
	session, ok := coreauth.FromContext(c.Request.Context())
	if !ok || session.User.Status != coreauth.StatusActive {
		fail(c, errs.New(errs.CodeUnauthorized, "authentication required"))
		return coreauth.Session{}, false
	}
	return session, true
}

// settingResponse 把一个名册条目转成传输形状。
// 敏感键不带 Value；非敏感键不带 Sensitive。
func settingResponse(item appsettings.Item) oapi.SettingItem {
	out := oapi.SettingItem{
		Key:    item.Key,
		Group:  item.Group,
		Source: oapi.SettingSource(item.Source),
	}
	if item.Sensitive != nil {
		out.Sensitive = &struct {
			Masked string             `json:"masked"`
			Set    bool               `json:"set"`
			Source oapi.SettingSource `json:"source"`
		}{
			Masked: item.Sensitive.Masked,
			Set:    item.Sensitive.Set,
			Source: oapi.SettingSource(item.Sensitive.Source),
		}
		return out
	}
	out.Value = settingValue(item.Value)
	return out
}

// settingValue 把解析值装进生成的一of 联合类型。
// 名册缺省值只允许标量；未知类型返回零值（JSON null），不猜测。
// 契约的 number 分支是 format: double，故 Go 侧是 float64，不做窄化。
func settingValue(value any) oapi.SettingItem_Value {
	var union oapi.SettingItem_Value
	switch typed := value.(type) {
	case string:
		_ = union.FromSettingItemValue0(typed)
	case bool:
		_ = union.FromSettingItemValue2(typed)
	case float64:
		_ = union.FromSettingItemValue1(typed)
	case int:
		_ = union.FromSettingItemValue1(float64(typed))
	case int64:
		_ = union.FromSettingItemValue1(float64(typed))
	}
	return union
}

// announcementResponse 把公告转成传输形状。
func announcementResponse(announcement appsettings.Announcement) oapi.Announcement {
	return oapi.Announcement{Text: announcement.Text, UpdatedAt: announcement.UpdatedAt}
}

func fail(c *gin.Context, err error) {
	respond.Error(c.Writer, c.Request, err)
}
