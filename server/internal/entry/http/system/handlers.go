// Package system 把设置名册、全站公告与测试邮件映射到 HTTP。
//
// 功能：
// GET /api/admin/settings（master/admin 只读名册，写回键报 source=db）、
// PUT /api/admin/schedule-settings（master 部分补丁写回调度键：sett2 三键
// ＋批 3b 四枚 DB-only 键——周末门两布尔与默认窗口两分钟＋批 3c 分布档一枚）、
// GET /api/admin/quota-settings（master/admin 只读配额两键生效值与来源）、
// PUT /api/admin/quota-settings（master 部分补丁写回配额两键：r4r）、
// POST /api/admin/mail/test（master 向自己的邮箱发测试信）、
// POST /api/admin/webhook/test（master 经配置的 webhook 出口发测试推送）、
// PUT /api/admin/announcement（master）、GET /api/announcement（所有已登录角色）。
// 本包**不**写 SQL、**不**回显敏感值。
//
// 归属：entry/http 入口层。用例来自 app/settings；
// 角色是运行时门：设置名册限 master/admin，调度设置写回、公告写与
// 测试邮件、测试推送限 master。
//
// 口径：敏感配置只回 {set, masked, source}；公告正文不经审计 detail。
// 测试邮件收件人只取会话用户邮箱，测试推送目标只取配置的出口；
// 响应只给固定文案，不含对端细节。
// 调度设置写回是部分补丁：缺字段=不动、null=删行回 env、值=upsert；
// 未知字段在这里显式 400，不被解码器静默忽略。
package system

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/auth"
	appsettings "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/settings"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
)

// Roster 是只读设置名册端口。
// 覆盖链存储故障原样上抛：故障不是配置状态。
type Roster interface {
	Report(ctx context.Context) (appsettings.Report, error)
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

// WebhookTest 是测试推送用例端口。
type WebhookTest interface {
	SendTestWebhook(ctx context.Context, user coreauth.User) error
}

// AutoApprove 是全局自动审批开关端口。
type AutoApprove interface {
	AutoApprove(ctx context.Context, scope corescope.Scope) (bool, error)
	SetAutoApprove(ctx context.Context, scope corescope.Scope, actor appaudit.Actor, enabled bool) (bool, error)
}

// ScheduleSettings 是调度设置写回端口（sett2）。
// 未注入时端点回 501：写面缺失必须显性。
type ScheduleSettings interface {
	SetSchedule(ctx context.Context, scope corescope.Scope, actor appaudit.Actor, changes []appsettings.ScheduleChange) error
}

// Reauth 是危险操作的二次口令端口。实现是 app/auth.Service。
// 缺失时写面 500 失败关闭：SMTP 凭据是出口配置，不得无复核落库。
type Reauth interface {
	Reauthenticate(ctx context.Context, userID, password string) error
}

// Handler 处理设置名册、公告、测试邮件、测试推送、自动审批开关与配额端点。
type Handler struct {
	roster           Roster
	announcement     Announcement
	mailTest         MailTest
	webhookTest      WebhookTest
	autoApprove      AutoApprove
	scheduleSettings ScheduleSettings
	quotaSettings    QuotaSettings
	mailSettings     MailSettings
	reauth           Reauth
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

// WithWebhookTest 注入测试推送用例。
// 未注入时端点回 501：生产装配必须注入，缺失必须显性。
func (h *Handler) WithWebhookTest(test WebhookTest) *Handler {
	h.webhookTest = test
	return h
}

// WithScheduleSettings 注入调度设置写回用例（sett2）。
// 未注入时端点回 501：生产装配必须注入，缺失必须显性。
func (h *Handler) WithScheduleSettings(s ScheduleSettings) *Handler {
	h.scheduleSettings = s
	return h
}

// WithReauth 注入危险操作的二次口令用例。
// 未注入时 mail 写面回 500：出口配置不得无复核落库。
func (h *Handler) WithReauth(r Reauth) *Handler {
	h.reauth = r
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
	report, err := h.roster.Report(c.Request.Context())
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, settingsReportResponse(report))
}

// settingsReportResponse 把名册报告转成传输形状。GET 与写回端点共用同一形态。
func settingsReportResponse(report appsettings.Report) oapi.SettingsReport {
	groups := make([]oapi.SettingGroup, 0, len(report.Groups))
	for _, group := range report.Groups {
		items := make([]oapi.SettingItem, 0, len(group.Items))
		for _, item := range group.Items {
			items = append(items, settingResponse(item))
		}
		groups = append(groups, oapi.SettingGroup{Name: group.Name, Items: items})
	}
	return oapi.SettingsReport{Groups: groups}
}

// AdminScheduleSettingsSet 实现 PUT /api/admin/schedule-settings。仅 master。
//
// 部分补丁语义：缺字段=不动该键；显式 null=删覆盖行回 env；值=upsert。
// 键存在性只能在解码原始 JSON 时判定（与 TimePrefSet 同因），
// 未知字段在这里显式拒绝（400），不被解码器静默忽略。
// 域校验与写入都在用例层：任何拒绝零写入零审计。
// 批 3b：默认窗口两键另有跨字段成对校验（单侧写入与生效值组合后
// 仍需 start<end，违规 400）。
func (h *Handler) AdminScheduleSettingsSet(c *gin.Context, _ oapi.AdminScheduleSettingsSetParams) {
	session, ok := requester(c)
	if !ok {
		return
	}
	if session.User.Role != coreauth.RoleMaster {
		fail(c, errs.New(errs.CodeForbidden, "master role is required"))
		return
	}
	if h.scheduleSettings == nil || h.roster == nil {
		respond.NotImplemented(c.Writer, c.Request)
		return
	}
	var body map[string]json.RawMessage
	if err := json.NewDecoder(c.Request.Body).Decode(&body); err != nil {
		respond.BindError(c.Writer, c.Request, err)
		return
	}
	changes, err := scheduleChangesFromJSON(body)
	if err != nil {
		fail(c, err)
		return
	}
	if err := h.scheduleSettings.SetSchedule(c.Request.Context(), scopeOf(session.User),
		appaudit.NewActor(session.User), changes); err != nil {
		fail(c, err)
		return
	}
	report, err := h.roster.Report(c.Request.Context())
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, settingsReportResponse(report))
}

// scheduleFieldKind 是 PUT 字段的值类型（解码分派用）：布尔字面、整数分钟
// 或分布档字符串枚举。
type scheduleFieldKind int

const (
	scheduleFieldInt scheduleFieldKind = iota
	scheduleFieldBool
	scheduleFieldSignDist
)

// scheduleBodyKeys 把 PUT 请求体字段名（对齐旧线设置页字段）映射到
// settings 表键名与值类型。批 3b 扩四枚 DB-only 键：周末门两布尔
// 与默认窗口两分钟；批 3c 扩分布档字符串枚举。表外字段一律未知（400）。
var scheduleBodyKeys = map[string]struct {
	key  string
	kind scheduleFieldKind
}{
	"edge_front_sec":         {key: appsettings.KeyScheduleEdgeFrontSec, kind: scheduleFieldInt},
	"edge_back_sec":          {key: appsettings.KeyScheduleEdgeBackSec, kind: scheduleFieldInt},
	"allow_time_pref":        {key: appsettings.KeyScheduleAllowTimePref, kind: scheduleFieldBool},
	"sunday_sign":            {key: appsettings.KeyScheduleSundaySign, kind: scheduleFieldBool},
	"saturday_sign":          {key: appsettings.KeyScheduleSaturdaySign, kind: scheduleFieldBool},
	"default_sign_start_min": {key: appsettings.KeyScheduleDefaultSignStartMin, kind: scheduleFieldInt},
	"default_sign_end_min":   {key: appsettings.KeyScheduleDefaultSignEndMin, kind: scheduleFieldInt},
	"sign_dist":              {key: appsettings.KeyScheduleSignDist, kind: scheduleFieldSignDist},
}

// scheduleChangesFromJSON 把原始 JSON 体折成写意图。
// 未知字段、JSON 类型错、空更新都在这里 400；域校验（含默认窗口的
// 跨字段成对校验）在用例层。
func scheduleChangesFromJSON(body map[string]json.RawMessage) ([]appsettings.ScheduleChange, error) {
	changes := make([]appsettings.ScheduleChange, 0, len(body))
	for field, raw := range body {
		spec, known := scheduleBodyKeys[field]
		if !known {
			return nil, errs.New(errs.CodeValidation, "unknown schedule setting key "+field)
		}
		if string(raw) == "null" {
			changes = append(changes, appsettings.ScheduleChange{Key: spec.key, Op: appsettings.ScheduleOpClear})
			continue
		}
		if spec.kind == scheduleFieldSignDist {
			// 分布档（批 3c）：JSON 字符串先过词表；大小写错或未知词 400。
			var text string
			if err := json.Unmarshal(raw, &text); err != nil {
				return nil, errs.New(errs.CodeValidation, field+" requires a string literal")
			}
			value, ok := appsettings.ParseSignDist(text)
			if !ok {
				return nil, errs.New(errs.CodeValidation, field+" must be front or uniform")
			}
			changes = append(changes, appsettings.ScheduleChange{Key: spec.key, Op: appsettings.ScheduleOpSet, Value: value})
			continue
		}
		if spec.kind == scheduleFieldBool {
			var value bool
			if err := json.Unmarshal(raw, &value); err != nil {
				return nil, errs.New(errs.CodeValidation, field+" requires a boolean literal")
			}
			changes = append(changes, appsettings.ScheduleChange{Key: spec.key, Op: appsettings.ScheduleOpSet, Value: value})
			continue
		}
		var value int
		if err := json.Unmarshal(raw, &value); err != nil {
			return nil, errs.New(errs.CodeValidation, field+" requires an integer")
		}
		changes = append(changes, appsettings.ScheduleChange{Key: spec.key, Op: appsettings.ScheduleOpSet, Value: value})
	}
	if len(changes) == 0 {
		return nil, errs.New(errs.CodeValidation, "schedule settings update is empty")
	}
	return changes, nil
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

// AdminWebhookTest 实现 POST /api/admin/webhook/test。仅 master。
// 请求体为空且被忽略：目标只取配置的 webhook 出口，客户端无法指定。
func (h *Handler) AdminWebhookTest(c *gin.Context, _ oapi.AdminWebhookTestParams) {
	session, ok := requester(c)
	if !ok {
		return
	}
	if session.User.Role != coreauth.RoleMaster {
		fail(c, errs.New(errs.CodeForbidden, "master role is required"))
		return
	}
	if h.webhookTest == nil {
		respond.NotImplemented(c.Writer, c.Request)
		return
	}
	if err := h.webhookTest.SendTestWebhook(c.Request.Context(), session.User); err != nil {
		retryAfter(c, err)
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, oapi.WebhookTestResult{
		Message: "test webhook accepted by the outlet",
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

// reauthenticate 校验危险操作的二次口令并写出失败响应。
// 缺值（空串）回 400；用例错误按码映射（403/429/500）；
// 节流命中时先写 Retry-After 头再回 429。
// 返回 false 表示已写响应，调用方直接返回。
// 口径与 account/admin/schedule 三个既有 helper 一致。
func (h *Handler) reauthenticate(c *gin.Context, userID, password string) bool {
	if h.reauth == nil {
		// 生产装配必须注入；缺失是接线错误，失败关闭。
		fail(c, errs.New(errs.CodeInternal, "reauthentication is not wired"))
		return false
	}
	if password == "" {
		fail(c, errs.New(errs.CodeValidation, "current_password is required"))
		return false
	}
	if err := h.reauth.Reauthenticate(c.Request.Context(), userID, password); err != nil {
		writeReauthRetryAfter(c, err)
		fail(c, err)
		return false
	}
	return true
}

// writeReauthRetryAfter 在再认证节流错误上写 Retry-After 头（秒，向上取整，至少 1）。
func writeReauthRetryAfter(c *gin.Context, err error) {
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

func fail(c *gin.Context, err error) {
	respond.Error(c.Writer, c.Request, err)
}
