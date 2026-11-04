// Package settings 把设置名册、通知订阅与全站公告映射到 HTTP。
//
// 功能：
// GET/PUT /api/admin/notify（master/admin 管理自己的订阅）、
// GET /api/admin/settings（master/admin 只读名册）、
// PUT /api/admin/announcement（master）、GET /api/announcement（所有已登录角色）。
// 本包**不**写 SQL、**不**投递通知、**不**回显敏感值。
//
// 归属：entry/http 入口层。用例来自 app/notify 与 app/settings；
// 角色是运行时门：设置与订阅限 master/admin，公告写限 master。
//
// 口径：通知订阅按会话 userID 收敛到本人，跨人不可见。
// 敏感配置只回 {set, masked, source}；公告正文不经审计 detail。
package settings

import (
	"context"
	"encoding/json"
	"net/http"

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

// Roster 是只读设置名册端口。
type Roster interface {
	Report() appsettings.Report
}

// Announcement 是公告用例端口。
type Announcement interface {
	Get(ctx context.Context) (appsettings.Announcement, error)
	Set(ctx context.Context, text string, actor appaudit.Actor) (appsettings.Announcement, error)
}

// Handler 处理通知订阅、设置名册与公告端点。
type Handler struct {
	notify       Notify
	roster       Roster
	announcement Announcement
}

// NewHandler 构造设置处理器。
func NewHandler(notify Notify, roster Roster, announcement Announcement) *Handler {
	return &Handler{notify: notify, roster: roster, announcement: announcement}
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
