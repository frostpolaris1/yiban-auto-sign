package settings_test

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	appnotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/notify"
	appsettings "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/settings"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coremail "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mail"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	settingshttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/settings"
)

// memNotifyRepo 是订阅覆盖集的内存实现。
type memNotifyRepo struct{ overrides map[corenotify.Pair]bool }

func (m *memNotifyRepo) Overrides(context.Context, string) (map[corenotify.Pair]bool, error) {
	out := make(map[corenotify.Pair]bool, len(m.overrides))
	for pair, enabled := range m.overrides {
		out[pair] = enabled
	}
	return out, nil
}

func (m *memNotifyRepo) Replace(_ context.Context, _ string, prefs []corenotify.Pref) error {
	m.overrides = make(map[corenotify.Pair]bool, len(prefs))
	for _, pref := range prefs {
		m.overrides[corenotify.Pair{Category: pref.Category, Channel: pref.Channel}] = pref.Enabled
	}
	return nil
}

type memAnnouncementStore struct {
	text      string
	updatedAt *time.Time
}

func (m *memAnnouncementStore) Announcement(context.Context) (string, *time.Time, error) {
	return m.text, m.updatedAt, nil
}

func (m *memAnnouncementStore) SetAnnouncement(_ context.Context, text, _ string) (*time.Time, error) {
	if text == "" {
		m.text, m.updatedAt = "", nil
		return nil, nil
	}
	now := time.Date(2026, 11, 4, 12, 0, 0, 0, time.UTC)
	m.text, m.updatedAt = text, &now
	return &now, nil
}

type memAudit struct{ entries []coreaudit.Entry }

func (m *memAudit) Append(_ context.Context, in coreaudit.Entry) (coreaudit.Row, error) {
	m.entries = append(m.entries, in)
	return coreaudit.Row{}, nil
}

func (m *memAudit) Verify(context.Context, int64, int64) (coreaudit.Report, error) {
	return coreaudit.Report{}, nil
}

type passthroughTx struct{}

func (passthroughTx) InTx(ctx context.Context, fn func(context.Context) error) error { return fn(ctx) }

type mapSource map[string]string

func (m mapSource) Lookup(key string) (string, bool) {
	value, ok := m[key]
	return value, ok
}

type fixedClock struct{}

func (fixedClock) Now() time.Time { return time.Date(2026, 11, 4, 9, 0, 0, 0, time.UTC) }

// fakeSender 记录最后一次投递，并按需返回错误。
type fakeSender struct {
	msg   coremail.Message
	err   error
	sends int
}

func (f *fakeSender) Send(_ context.Context, msg coremail.Message) error {
	f.sends++
	f.msg = msg
	return f.err
}

// fakeCooldown 是固定判据的冷却替身。
type fakeCooldown struct {
	allowed bool
	wait    time.Duration
	keys    []string
}

func (f *fakeCooldown) Allow(key string) (bool, time.Duration) {
	f.keys = append(f.keys, key)
	return f.allowed, f.wait
}

func newHandler(t *testing.T) (*settingshttp.Handler, *memAnnouncementStore, *memAudit) {
	t.Helper()
	return newHandlerWith(t, nil, nil)
}

func newHandlerWith(t *testing.T, sender coremail.Sender, cooldown appsettings.Cooldown) (*settingshttp.Handler, *memAnnouncementStore, *memAudit) {
	t.Helper()
	notifySvc, err := appnotify.NewService(&memNotifyRepo{})
	if err != nil {
		t.Fatalf("notify service: %v", err)
	}
	roster, err := appsettings.NewRoster([]appsettings.Entry{
		{Key: "YIBAN_WORKERS", Group: "调度", Default: float64(1), HasDefault: true},
		{Key: "YIBAN_MAIL_PASS", Group: "发布", Sensitive: true},
	}, mapSource{"YIBAN_MAIL_PASS": "smtp-secret"})
	if err != nil {
		t.Fatalf("roster: %v", err)
	}
	store := &memAnnouncementStore{}
	audits := &memAudit{}
	announcementSvc, err := appsettings.NewService(appsettings.Deps{
		Store: store, Audit: audits, Tx: passthroughTx{}, Clock: fixedClock{},
		Mail: sender, Cooldown: cooldown,
	})
	if err != nil {
		t.Fatalf("announcement service: %v", err)
	}
	return settingshttp.NewHandler(notifySvc, roster, announcementSvc, announcementSvc), store, audits
}

func newContext(method, path, body string, role coreauth.Role) (*gin.Context, *httptest.ResponseRecorder) {
	return newContextWithEmail(method, path, body, role, "admin@example.com")
}

func newContextWithEmail(method, path, body string, role coreauth.Role,
	email string) (*gin.Context, *httptest.ResponseRecorder) {
	gin.SetMode(gin.TestMode)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	if role != "" {
		req = req.WithContext(coreauth.WithSession(req.Context(), coreauth.Session{
			User: coreauth.User{
				ID: "11111111-1111-1111-1111-111111111111", Email: email,
				Role: role, Status: coreauth.StatusActive,
			},
			CSRFToken: "csrf",
		}))
	}
	c.Request = req
	return c, rec
}

func errorCode(t *testing.T, rec *httptest.ResponseRecorder) string {
	t.Helper()
	var envelope struct {
		Error struct {
			Code string `json:"code"`
		} `json:"error"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &envelope); err != nil {
		t.Fatalf("decode error envelope: %v; body=%q", err, rec.Body.String())
	}
	return envelope.Error.Code
}

// 未登录 401；user 403；master/admin 200。
func TestNotifyRoleGate(t *testing.T) {
	handler, _, _ := newHandler(t)
	c, rec := newContext(http.MethodGet, "/api/admin/notify", "", "")
	handler.AdminNotifyGet(c)
	if rec.Code != http.StatusUnauthorized || errorCode(t, rec) != "unauthorized" {
		t.Fatalf("anonymous = %d %s", rec.Code, rec.Body.String())
	}

	c, rec = newContext(http.MethodGet, "/api/admin/notify", "", coreauth.RoleUser)
	handler.AdminNotifyGet(c)
	if rec.Code != http.StatusForbidden || errorCode(t, rec) != "forbidden" {
		t.Fatalf("user = %d %s", rec.Code, rec.Body.String())
	}

	for _, role := range []coreauth.Role{coreauth.RoleAdmin, coreauth.RoleMaster} {
		c, rec = newContext(http.MethodGet, "/api/admin/notify", "", role)
		handler.AdminNotifyGet(c)
		if rec.Code != http.StatusOK {
			t.Fatalf("%s = %d %s", role, rec.Code, rec.Body.String())
		}
		var page struct {
			Items []struct {
				Category string `json:"category"`
				Channel  string `json:"channel"`
				Enabled  bool   `json:"enabled"`
			} `json:"items"`
		}
		if err := json.Unmarshal(rec.Body.Bytes(), &page); err != nil {
			t.Fatalf("decode page: %v", err)
		}
		if len(page.Items) != 8 {
			t.Fatalf("%s items = %d, want 8", role, len(page.Items))
		}
	}
}

// 负向：未知类别/渠道、重复对、缺失必填字段都是 400。
func TestNotifySetRejectsInvalid(t *testing.T) {
	handler, _, _ := newHandler(t)
	cases := map[string]string{
		"unknown category": `{"items":[{"category":"weekly_digest","channel":"in_app","enabled":true}]}`,
		"unknown channel":  `{"items":[{"category":"review_pending","channel":"sms","enabled":true}]}`,
		"duplicate":        `{"items":[{"category":"review_pending","channel":"email","enabled":true},{"category":"review_pending","channel":"email","enabled":false}]}`,
		"missing items":    `{}`,
		"null items":       `{"items":null}`,
		"missing enabled":  `{"items":[{"category":"review_pending","channel":"email"}]}`,
		"null enabled":     `{"items":[{"category":"review_pending","channel":"email","enabled":null}]}`,
		"missing category": `{"items":[{"channel":"email","enabled":true}]}`,
		"missing channel":  `{"items":[{"category":"review_pending","enabled":true}]}`,
	}
	for name, body := range cases {
		t.Run(name, func(t *testing.T) {
			c, rec := newContext(http.MethodPut, "/api/admin/notify", body, coreauth.RoleAdmin)
			handler.AdminNotifySet(c, oapi.AdminNotifySetParams{})
			if rec.Code != http.StatusBadRequest || errorCode(t, rec) != "validation_failed" {
				t.Fatalf("status = %d body=%s", rec.Code, rec.Body.String())
			}
		})
	}
}

// PUT 是完整替换，响应即替换后的有效状态。
func TestNotifySetReplaces(t *testing.T) {
	handler, _, _ := newHandler(t)
	c, rec := newContext(http.MethodPut, "/api/admin/notify",
		`{"items":[{"category":"review_pending","channel":"email","enabled":false}]}`,
		coreauth.RoleAdmin)
	handler.AdminNotifySet(c, oapi.AdminNotifySetParams{})
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d body=%s", rec.Code, rec.Body.String())
	}
	var page struct {
		Items []struct {
			Category string `json:"category"`
			Channel  string `json:"channel"`
			Enabled  bool   `json:"enabled"`
		} `json:"items"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &page); err != nil {
		t.Fatalf("decode page: %v", err)
	}
	for _, item := range page.Items {
		if item.Category == "review_pending" && item.Channel == "email" && item.Enabled {
			t.Fatal("stored override false was ignored")
		}
		if item.Category == "capacity_rejected" && item.Channel == "in_app" && !item.Enabled {
			t.Fatal("unlisted pair did not fall back to its default")
		}
	}
}

// 设置名册：user 403；admin 200；敏感键不回显原值。
func TestSettingsRoleGateAndMasking(t *testing.T) {
	handler, _, _ := newHandler(t)
	c, rec := newContext(http.MethodGet, "/api/admin/settings", "", coreauth.RoleUser)
	handler.AdminSettings(c)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("user = %d", rec.Code)
	}

	c, rec = newContext(http.MethodGet, "/api/admin/settings", "", coreauth.RoleAdmin)
	handler.AdminSettings(c)
	if rec.Code != http.StatusOK {
		t.Fatalf("admin = %d body=%s", rec.Code, rec.Body.String())
	}
	if strings.Contains(rec.Body.String(), "smtp-secret") {
		t.Fatal("sensitive value leaked into the response")
	}
	var report struct {
		Groups []struct {
			Name  string `json:"name"`
			Items []struct {
				Key       string          `json:"key"`
				Source    string          `json:"source"`
				Value     json.RawMessage `json:"value"`
				Sensitive *struct {
					Set    bool   `json:"set"`
					Masked string `json:"masked"`
					Source string `json:"source"`
				} `json:"sensitive"`
			} `json:"items"`
		} `json:"groups"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &report); err != nil {
		t.Fatalf("decode report: %v", err)
	}
	items := map[string]json.RawMessage{}
	sensitive := map[string]*struct {
		Set    bool   `json:"set"`
		Masked string `json:"masked"`
		Source string `json:"source"`
	}{}
	for _, group := range report.Groups {
		for _, item := range group.Items {
			items[item.Key] = item.Value
			sensitive[item.Key] = item.Sensitive
		}
	}
	if got := string(items["YIBAN_WORKERS"]); got != "1" {
		t.Fatalf("YIBAN_WORKERS value = %s, want the registered default 1", got)
	}
	if got := string(items["YIBAN_MAIL_PASS"]); got != "null" {
		t.Fatalf("sensitive value = %s, want null", got)
	}
	view := sensitive["YIBAN_MAIL_PASS"]
	if view == nil || !view.Set || view.Masked != "****" || view.Source != "env" {
		t.Fatalf("sensitive view = %+v", view)
	}
}

// 公告读：所有已登录角色 200；未登录 401。
func TestAnnouncementReadAllRoles(t *testing.T) {
	handler, _, _ := newHandler(t)
	c, rec := newContext(http.MethodGet, "/api/announcement", "", "")
	handler.AnnouncementGet(c)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("anonymous = %d", rec.Code)
	}
	for _, role := range []coreauth.Role{coreauth.RoleUser, coreauth.RoleAdmin, coreauth.RoleMaster} {
		c, rec = newContext(http.MethodGet, "/api/announcement", "", role)
		handler.AnnouncementGet(c)
		if rec.Code != http.StatusOK {
			t.Fatalf("%s = %d body=%s", role, rec.Code, rec.Body.String())
		}
		var body struct {
			Text      string  `json:"text"`
			UpdatedAt *string `json:"updated_at"`
		}
		if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
			t.Fatalf("decode announcement: %v", err)
		}
		if body.Text != "" || body.UpdatedAt != nil {
			t.Fatalf("%s announcement = %+v, want empty/null", role, body)
		}
	}
}

// 公告写：仅 master；正文超长或换行 400；缺 text 400；成功写审计。
func TestAnnouncementWriteMasterOnly(t *testing.T) {
	handler, store, audits := newHandler(t)
	for _, role := range []coreauth.Role{coreauth.RoleUser, coreauth.RoleAdmin} {
		c, rec := newContext(http.MethodPut, "/api/admin/announcement", `{"text":"公告"}`, role)
		handler.AnnouncementSet(c, oapi.AnnouncementSetParams{})
		if rec.Code != http.StatusForbidden {
			t.Fatalf("%s = %d", role, rec.Code)
		}
	}
	if store.text != "" || len(audits.entries) != 0 {
		t.Fatal("forbidden write reached storage")
	}

	cases := map[string]string{
		"missing text": `{}`,
		"too long":     `{"text":"` + strings.Repeat("字", 201) + `"}`,
		"line feed":    `{"text":"第一行\n第二行"}`,
	}
	for name, body := range cases {
		t.Run(name, func(t *testing.T) {
			c, rec := newContext(http.MethodPut, "/api/admin/announcement", body, coreauth.RoleMaster)
			handler.AnnouncementSet(c, oapi.AnnouncementSetParams{})
			if rec.Code != http.StatusBadRequest {
				t.Fatalf("status = %d body=%s", rec.Code, rec.Body.String())
			}
		})
	}
	if store.text != "" || len(audits.entries) != 0 {
		t.Fatal("invalid body reached storage")
	}

	c, rec := newContext(http.MethodPut, "/api/admin/announcement", `{"text":"  公告正文  "}`, coreauth.RoleMaster)
	handler.AnnouncementSet(c, oapi.AnnouncementSetParams{})
	if rec.Code != http.StatusOK {
		t.Fatalf("master = %d body=%s", rec.Code, rec.Body.String())
	}
	if store.text != "公告正文" {
		t.Fatalf("stored = %q, want trimmed", store.text)
	}
	if len(audits.entries) != 1 || audits.entries[0].Action != coreaudit.ActionSettingsAnnouncement {
		t.Fatalf("audit = %+v", audits.entries)
	}
}

// 公告写到清空：空正文回空文本与 null 时间。
func TestAnnouncementClear(t *testing.T) {
	handler, store, _ := newHandler(t)
	store.text = "旧公告"
	c, rec := newContext(http.MethodPut, "/api/admin/announcement", `{"text":""}`, coreauth.RoleMaster)
	handler.AnnouncementSet(c, oapi.AnnouncementSetParams{})
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d", rec.Code)
	}
	if store.text != "" {
		t.Fatalf("stored = %q, want cleared", store.text)
	}
	var body struct {
		Text      string  `json:"text"`
		UpdatedAt *string `json:"updated_at"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.Text != "" || body.UpdatedAt != nil {
		t.Fatalf("body = %+v", body)
	}
}

// 测试邮件：未登录 401；admin/user 403；master 200 且固定文案。
func TestMailTestMasterOnly(t *testing.T) {
	handler, _, _ := newHandlerWith(t, &fakeSender{}, nil)

	c, rec := newContext(http.MethodPost, "/api/admin/mail/test", "", "")
	handler.AdminMailTest(c, oapi.AdminMailTestParams{})
	if rec.Code != http.StatusUnauthorized || errorCode(t, rec) != "unauthorized" {
		t.Fatalf("anonymous = %d %s", rec.Code, rec.Body.String())
	}
	for _, role := range []coreauth.Role{coreauth.RoleUser, coreauth.RoleAdmin} {
		c, rec = newContext(http.MethodPost, "/api/admin/mail/test", "", role)
		handler.AdminMailTest(c, oapi.AdminMailTestParams{})
		if rec.Code != http.StatusForbidden || errorCode(t, rec) != "forbidden" {
			t.Fatalf("%s = %d %s", role, rec.Code, rec.Body.String())
		}
	}

	c, rec = newContext(http.MethodPost, "/api/admin/mail/test", "", coreauth.RoleMaster)
	handler.AdminMailTest(c, oapi.AdminMailTestParams{})
	if rec.Code != http.StatusOK {
		t.Fatalf("master = %d %s", rec.Code, rec.Body.String())
	}
	var body struct {
		Message string `json:"message"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.Message == "" {
		t.Fatal("success answer carries no message")
	}
}

// 收件人只取会话邮箱：请求体里的 to 被忽略。
func TestMailTestUsesSessionMailbox(t *testing.T) {
	sender := &fakeSender{}
	handler, _, _ := newHandlerWith(t, sender, &fakeCooldown{allowed: true})

	c, rec := newContextWithEmail(http.MethodPost, "/api/admin/mail/test",
		`{"to":"attacker@example.com"}`, coreauth.RoleMaster, "owner@example.com")
	handler.AdminMailTest(c, oapi.AdminMailTestParams{})
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d body=%s", rec.Code, rec.Body.String())
	}
	if sender.sends != 1 {
		t.Fatalf("sends = %d, want 1", sender.sends)
	}
	if sender.msg.To != "owner@example.com" {
		t.Fatalf("recipient = %q, want the session mailbox", sender.msg.To)
	}
	if sender.msg.Subject == "" || sender.msg.Text == "" {
		t.Fatalf("message = %+v, want subject and body", sender.msg)
	}
}

// 失败映射：未配置 409、无邮箱 409、发送失败 502、冷却 429＋Retry-After。
// 响应体不得出现 SMTP 应答或口令。
func TestMailTestErrorMapping(t *testing.T) {
	t.Run("mail disabled", func(t *testing.T) {
		handler, _, _ := newHandlerWith(t, nil, nil)
		c, rec := newContext(http.MethodPost, "/api/admin/mail/test", "", coreauth.RoleMaster)
		handler.AdminMailTest(c, oapi.AdminMailTestParams{})
		if rec.Code != http.StatusConflict || errorCode(t, rec) != "conflict" {
			t.Fatalf("status = %d %s", rec.Code, rec.Body.String())
		}
	})

	t.Run("no email", func(t *testing.T) {
		sender := &fakeSender{}
		handler, _, _ := newHandlerWith(t, sender, nil)
		c, rec := newContextWithEmail(http.MethodPost, "/api/admin/mail/test",
			"", coreauth.RoleMaster, "")
		handler.AdminMailTest(c, oapi.AdminMailTestParams{})
		if rec.Code != http.StatusConflict || errorCode(t, rec) != "conflict" {
			t.Fatalf("status = %d %s", rec.Code, rec.Body.String())
		}
		if sender.sends != 0 {
			t.Fatal("a mailbox-less account reached the sender")
		}
	})

	t.Run("send failed", func(t *testing.T) {
		const secret = "smtp-secret-9f21"
		sender := &fakeSender{err: &coremail.DeliveryError{
			Kind: coremail.KindAuth, Err: errors.New(secret),
		}}
		handler, _, _ := newHandlerWith(t, sender, nil)
		c, rec := newContext(http.MethodPost, "/api/admin/mail/test", "", coreauth.RoleMaster)
		handler.AdminMailTest(c, oapi.AdminMailTestParams{})
		if rec.Code != http.StatusBadGateway || errorCode(t, rec) != "upstream_unavailable" {
			t.Fatalf("status = %d %s", rec.Code, rec.Body.String())
		}
		if strings.Contains(rec.Body.String(), secret) || strings.Contains(rec.Body.String(), "kind=") {
			t.Fatalf("delivery detail leaked: %s", rec.Body.String())
		}
	})

	t.Run("cooldown", func(t *testing.T) {
		sender := &fakeSender{}
		cooldown := &fakeCooldown{allowed: false, wait: 42 * time.Second}
		handler, _, _ := newHandlerWith(t, sender, cooldown)
		c, rec := newContext(http.MethodPost, "/api/admin/mail/test", "", coreauth.RoleMaster)
		handler.AdminMailTest(c, oapi.AdminMailTestParams{})
		if rec.Code != http.StatusTooManyRequests || errorCode(t, rec) != "rate_limited" {
			t.Fatalf("status = %d %s", rec.Code, rec.Body.String())
		}
		if got := rec.Header().Get("Retry-After"); got != "42" {
			t.Fatalf("Retry-After = %q, want 42", got)
		}
		if sender.sends != 0 {
			t.Fatal("a throttled request reached the sender")
		}
		if len(cooldown.keys) != 1 || !strings.HasPrefix(cooldown.keys[0], "admin-mail-test:") {
			t.Fatalf("cooldown keys = %v", cooldown.keys)
		}
	})
}
