// Package settings 把设置名册、通知订阅、站内信、全站公告与测试邮件映射到 HTTP。
//
// 功能：
// GET/PUT /api/admin/notify（master/admin 管理自己的订阅）、
// GET /api/me/notifications（master/admin 读本人站内信）、
// GET /api/me/notifications/unread-count、
// POST /api/me/notifications/read、POST /api/me/notifications/read-all、
// GET /api/admin/settings（master/admin 只读名册）、
// POST /api/admin/mail/test（master 向自己的邮箱发测试信）、
// PUT /api/admin/announcement（master）、GET /api/announcement（所有已登录角色）。
// 本包**不**写 SQL、**不**投递通知、**不**回显敏感值。
//
// 归属：entry/http 入口层。用例来自 app/notify 与 app/settings；
// 角色是运行时门：设置、订阅与站内信限 master/admin，公告写与测试邮件限 master。
//
// 口径：通知订阅与站内信都按会话 userID 收敛到本人，跨人不可见。
// 敏感配置只回 {set, masked, source}；公告正文不经审计 detail。
// 测试邮件收件人只取会话用户邮箱，响应只给固定文案。
package settings

import (
	"context"
	"encoding/json"
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appnotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/notify"
	appsettings "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/settings"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
)

// Notify 是通知订阅用例端口。
type Notify interface {
	Get(ctx context.Context, userID string) (appnotify.Page, error)
	Set(ctx context.Context, userID string, items []appnotify.Pref) (appnotify.Page, error)
}

// Inbox 是站内信读用例端口。全部方法只作用于会话用户。
type Inbox interface {
	List(ctx context.Context, userID, cursor string, limit int) (appnotify.NotificationPage, error)
	UnreadCount(ctx context.Context, userID string) (int64, error)
	MarkRead(ctx context.Context, userID string, ids []string) error
	MarkAllRead(ctx context.Context, userID string) error
}

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

// Handler 处理通知订阅、站内信、设置名册、公告与测试邮件端点。
type Handler struct {
	notify       Notify
	inbox        Inbox
	roster       Roster
	announcement Announcement
	mailTest     MailTest
}

// NewHandler 构造设置处理器。
func NewHandler(notify Notify, roster Roster, announcement Announcement, mailTest MailTest) *Handler {
	return &Handler{notify: notify, roster: roster, announcement: announcement, mailTest: mailTest}
}

// WithInbox 注入站内信读用例。
// 未注入时站内信端点回 501；生产装配必须注入。
func (h *Handler) WithInbox(inbox Inbox) *Handler {
	h.inbox = inbox
	return h
}

// AdminNotifyGet 实现 GET /api/admin/notify。
func (h *Handler) AdminNotifyGet(c *gin.Context) {
	session, ok := requester(c)
	if !ok {
		return
	}
	if !h.allowAdmin(c, session) {
		return
	}
	page, err := h.notify.Get(c.Request.Context(), session.User.ID)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, notifyResponse(page))
}

// AdminNotifySet 实现 PUT /api/admin/notify。
func (h *Handler) AdminNotifySet(c *gin.Context, _ oapi.AdminNotifySetParams) {
	session, ok := requester(c)
	if !ok {
		return
	}
	if !h.allowAdmin(c, session) {
		return
	}
	// 三个字段都是契约 required：用指针区分「缺失/null」与零值。
	// items 缺失或 null 不得等同于「清空全部覆盖」；
	// enabled 缺失不得静默当成 false。
	var body struct {
		Items *[]struct {
			Category *string `json:"category"`
			Channel  *string `json:"channel"`
			Enabled  *bool   `json:"enabled"`
		} `json:"items"`
	}
	if err := json.NewDecoder(c.Request.Body).Decode(&body); err != nil {
		respond.BindError(c.Writer, c.Request, err)
		return
	}
	if body.Items == nil {
		fail(c, errs.New(errs.CodeValidation, "items is required"))
		return
	}
	// [] 解码为指向空切片的非 nil 指针，表示回到全默认。
	items := make([]appnotify.Pref, 0, len(*body.Items))
	for _, item := range *body.Items {
		if item.Category == nil {
			fail(c, errs.New(errs.CodeValidation, "category is required"))
			return
		}
		if item.Channel == nil {
			fail(c, errs.New(errs.CodeValidation, "channel is required"))
			return
		}
		if item.Enabled == nil {
			fail(c, errs.New(errs.CodeValidation, "enabled is required"))
			return
		}
		items = append(items, appnotify.Pref{
			Category: appnotify.Category(*item.Category),
			Channel:  appnotify.Channel(*item.Channel),
			Enabled:  *item.Enabled,
		})
	}
	page, err := h.notify.Set(c.Request.Context(), session.User.ID, items)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, notifyResponse(page))
}

// NotificationList 实现 GET /api/me/notifications。
// 只读会话用户自己的行；cursor 与 limit 原样交给用例定型。
func (h *Handler) NotificationList(c *gin.Context, params oapi.NotificationListParams) {
	session, ok := requester(c)
	if !ok {
		return
	}
	if !h.allowAdmin(c, session) {
		return
	}
	if h.inbox == nil {
		respond.NotImplemented(c.Writer, c.Request)
		return
	}
	limit := appnotify.DefaultNotificationPageSize
	if params.Limit != nil {
		limit = *params.Limit
	}
	cursor := ""
	if params.Cursor != nil {
		cursor = *params.Cursor
	}
	page, err := h.inbox.List(c.Request.Context(), session.User.ID, cursor, limit)
	if err != nil {
		fail(c, err)
		return
	}
	items := make([]oapi.Notification, 0, len(page.Items))
	for _, item := range page.Items {
		items = append(items, notificationResponse(item))
	}
	// next_cursor 恒在：末页发空串，与其余列表页同一形状。
	out := oapi.NotificationPage{Items: items, NextCursor: page.NextCursor}
	respond.JSON(c.Writer, http.StatusOK, out)
}

// NotificationUnreadCount 实现 GET /api/me/notifications/unread-count。
func (h *Handler) NotificationUnreadCount(c *gin.Context) {
	session, ok := requester(c)
	if !ok {
		return
	}
	if !h.allowAdmin(c, session) {
		return
	}
	if h.inbox == nil {
		respond.NotImplemented(c.Writer, c.Request)
		return
	}
	count, err := h.inbox.UnreadCount(c.Request.Context(), session.User.ID)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, oapi.UnreadCount{Count: int(count)})
}

// NotificationMarkRead 实现 POST /api/me/notifications/read。
// ids 必填且非空；越权或不存在 id 由用例静默忽略，响应仍是 204。
func (h *Handler) NotificationMarkRead(c *gin.Context, _ oapi.NotificationMarkReadParams) {
	session, ok := requester(c)
	if !ok {
		return
	}
	if !h.allowAdmin(c, session) {
		return
	}
	if h.inbox == nil {
		respond.NotImplemented(c.Writer, c.Request)
		return
	}
	var body struct {
		Ids *[]string `json:"ids"`
	}
	if err := json.NewDecoder(c.Request.Body).Decode(&body); err != nil {
		respond.BindError(c.Writer, c.Request, err)
		return
	}
	if body.Ids == nil {
		fail(c, errs.New(errs.CodeValidation, "ids is required"))
		return
	}
	if err := h.inbox.MarkRead(c.Request.Context(), session.User.ID, *body.Ids); err != nil {
		fail(c, err)
		return
	}
	c.Status(http.StatusNoContent)
}

// NotificationMarkAllRead 实现 POST /api/me/notifications/read-all。
// 已全部已读时仍回 204：重复调用幂等。
func (h *Handler) NotificationMarkAllRead(c *gin.Context, _ oapi.NotificationMarkAllReadParams) {
	session, ok := requester(c)
	if !ok {
		return
	}
	if !h.allowAdmin(c, session) {
		return
	}
	if h.inbox == nil {
		respond.NotImplemented(c.Writer, c.Request)
		return
	}
	if err := h.inbox.MarkAllRead(c.Request.Context(), session.User.ID); err != nil {
		fail(c, err)
		return
	}
	c.Status(http.StatusNoContent)
}

// notificationResponse 把用例行转成传输形状。
// school_id 无值发 null；read_at 未读发 null；dedupe_key 不暴露。
func notificationResponse(item appnotify.Notification) oapi.Notification {
	out := oapi.Notification{
		Id:        item.ID,
		Category:  oapi.NotifyCategory(item.Category),
		Title:     item.Title,
		Body:      item.Body,
		CreatedAt: item.CreatedAt,
		ReadAt:    item.ReadAt,
	}
	if item.SchoolID != "" {
		schoolID := item.SchoolID
		out.SchoolId = &schoolID
	}
	return out
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

// notifyResponse 把用例页转成传输形状。
func notifyResponse(page appnotify.Page) oapi.NotifyPrefPage {
	items := make([]oapi.NotifyPref, 0, len(page.Items))
	for _, pref := range page.Items {
		items = append(items, oapi.NotifyPref{
			Category: oapi.NotifyCategory(pref.Category),
			Channel:  oapi.NotifyChannel(pref.Channel),
			Enabled:  pref.Enabled,
		})
	}
	return oapi.NotifyPrefPage{Items: items}
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
