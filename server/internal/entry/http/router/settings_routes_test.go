package router_test

// 设置面拆包后的接线门。
//
// 断言：通知与系统两端点的处理器都真的挂上路由；未登录 401、user 403、
// master/admin 按契约放行；写端点缺 CSRF 令牌 403；未装配时每端点 501。
// 本文件不连数据库：端口用进程内替身，判据是「路由与角色门咬住接线」。

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appnotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/notify"
	appsettings "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/settings"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	audithttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/audit"
	notifyhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/notify"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/router"
	systemhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/system"
)

type stubNotify struct{}

func (stubNotify) Get(context.Context, string) (appnotify.Page, error) {
	items := []appnotify.Pref{}
	for _, category := range corenotify.Categories() {
		for _, channel := range corenotify.Channels() {
			items = append(items, appnotify.Pref{
				Category: category, Channel: channel,
				Enabled: corenotify.Default(corenotify.Pair{Category: category, Channel: channel}),
			})
		}
	}
	return appnotify.Page{Items: items}, nil
}

func (s stubNotify) Set(ctx context.Context, userID string, _ []appnotify.Pref) (appnotify.Page, error) {
	return s.Get(ctx, userID)
}

type stubInbox struct{}

func (stubInbox) List(context.Context, string, string, int) (appnotify.NotificationPage, error) {
	return appnotify.NotificationPage{Items: []corenotify.Notification{}}, nil
}

func (stubInbox) UnreadCount(context.Context, string) (int64, error) { return 0, nil }

func (stubInbox) MarkRead(context.Context, string, []string) error { return nil }

func (stubInbox) MarkAllRead(context.Context, string) error { return nil }

type stubRoster struct{}

func (stubRoster) Report() appsettings.Report { return appsettings.Report{} }

type stubAnnouncement struct{ text string }

func (s *stubAnnouncement) Get(context.Context) (appsettings.Announcement, error) {
	return appsettings.Announcement{Text: s.text}, nil
}

func (s *stubAnnouncement) Set(_ context.Context, text string, _ appaudit.Actor) (appsettings.Announcement, error) {
	s.text = text
	return appsettings.Announcement{Text: text}, nil
}

type stubAuditReader struct{}

func (stubAuditReader) List(context.Context, appaudit.ListFilter) (appaudit.LogPage, error) {
	return appaudit.LogPage{}, nil
}

type stubMailTest struct{}

func (stubMailTest) SendTestMail(context.Context, auth.User) error { return nil }

// stubWebhookTest 是测试推送用例的内存替身。
type stubWebhookTest struct{}

func (stubWebhookTest) SendTestWebhook(context.Context, auth.User) error { return nil }

// stubAutoApprove 是自动审批开关的内存替身。
type stubAutoApprove struct{ enabled bool }

func (s *stubAutoApprove) AutoApprove(context.Context, corescope.Scope) (bool, error) {
	return s.enabled, nil
}

func (s *stubAutoApprove) SetAutoApprove(_ context.Context, _ corescope.Scope, _ appaudit.Actor, enabled bool) (bool, error) {
	s.enabled = enabled
	return enabled, nil
}

func newSettingsEngine(t *testing.T) (*gin.Engine, *stubIssuer) {
	t.Helper()
	return newSettingsEngineWith(t,
		notifyhttp.NewHandler(stubNotify{}).WithInbox(stubInbox{}),
		systemhttp.NewHandler(stubRoster{}, &stubAnnouncement{}, stubMailTest{}).
			WithAutoApprove(&stubAutoApprove{}).
			WithWebhookTest(stubWebhookTest{}))
}

func newSettingsEngineWith(t *testing.T, notify *notifyhttp.Handler, system *systemhttp.Handler) (*gin.Engine, *stubIssuer) {
	t.Helper()
	issuer := newStubIssuer()
	gin.SetMode(gin.TestMode)
	engine, err := router.New(router.Options{
		Sessions: issuer,
		Notify:   notify,
		System:   system,
		Audit:    audithttp.NewHandler(stubAuditReader{}),
	})
	if err != nil {
		t.Fatalf("router.New: %v", err)
	}
	return engine, issuer
}

func issueUser(t *testing.T, issuer *stubIssuer, role auth.Role) auth.Session {
	t.Helper()
	issued, err := issuer.Issue(context.Background(), auth.User{
		ID: "user-" + string(role), Email: string(role) + "@example.com",
		Role: role, Status: auth.StatusActive,
	})
	if err != nil {
		t.Fatalf("issue %s: %v", role, err)
	}
	return issued.Session
}

func requestWithSession(method, path, body, csrf string, session *auth.Session) *http.Request {
	var req *http.Request
	if body == "" {
		req = httptest.NewRequest(method, path, nil)
	} else {
		req = httptest.NewRequest(method, path, strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
	}
	if session != nil {
		req.AddCookie(&http.Cookie{Name: auth.CookieName, Value: "token-" + session.User.ID})
	}
	if csrf != "" {
		req.Header.Set("X-CSRF-Token", csrf)
	}
	return req
}

func TestSettingsRoutesAreWired(t *testing.T) {
	engine, issuer := newSettingsEngine(t)

	// 未登录：读端点都 401；写端点是 POST/PUT，缺 CSRF 先 403。
	for _, path := range []string{
		"/api/admin/notify", "/api/admin/settings", "/api/announcement", "/api/audit-logs",
		"/api/me/notifications", "/api/me/notifications/unread-count",
	} {
		rec := serve(engine, requestWithSession(http.MethodGet, path, "", "", nil))
		if rec.Code != http.StatusUnauthorized {
			t.Fatalf("%s anonymous = %d", path, rec.Code)
		}
	}
	rec := serve(engine, requestWithSession(http.MethodPost, "/api/admin/mail/test", "", "", nil))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("mail test anonymous = %d", rec.Code)
	}

	user := issueUser(t, issuer, auth.RoleUser)
	admin := issueUser(t, issuer, auth.RoleAdmin)
	master := issueUser(t, issuer, auth.RoleMaster)

	const notificationID = "018f4a4e-0000-7000-8000-000000000001"
	readBody := `{"ids":["` + notificationID + `"]}`
	cases := []struct {
		name    string
		method  string
		path    string
		body    string
		session auth.Session
		csrf    bool
		status  int
	}{
		{"notify user", http.MethodGet, "/api/admin/notify", "", user, false, http.StatusForbidden},
		{"notify admin", http.MethodGet, "/api/admin/notify", "", admin, false, http.StatusOK},
		{"notify master", http.MethodGet, "/api/admin/notify", "", master, false, http.StatusOK},
		{"notify put without csrf", http.MethodPut, "/api/admin/notify", `{"items":[]}`, admin, false, http.StatusForbidden},
		{"notify put admin", http.MethodPut, "/api/admin/notify", `{"items":[]}`, admin, true, http.StatusOK},
		{"notifications list user", http.MethodGet, "/api/me/notifications", "", user, false, http.StatusForbidden},
		{"notifications list admin", http.MethodGet, "/api/me/notifications", "", admin, false, http.StatusOK},
		{"notifications list master", http.MethodGet, "/api/me/notifications", "", master, false, http.StatusOK},
		{"notifications count user", http.MethodGet, "/api/me/notifications/unread-count", "", user, false, http.StatusForbidden},
		{"notifications count admin", http.MethodGet, "/api/me/notifications/unread-count", "", admin, false, http.StatusOK},
		{"notifications read user", http.MethodPost, "/api/me/notifications/read", readBody, user, true, http.StatusForbidden},
		{"notifications read without csrf", http.MethodPost, "/api/me/notifications/read", readBody, admin, false, http.StatusForbidden},
		{"notifications read admin", http.MethodPost, "/api/me/notifications/read", readBody, admin, true, http.StatusNoContent},
		{"notifications read-all user", http.MethodPost, "/api/me/notifications/read-all", "", user, true, http.StatusForbidden},
		{"notifications read-all admin", http.MethodPost, "/api/me/notifications/read-all", "", admin, true, http.StatusNoContent},
		{"notifications read-all master", http.MethodPost, "/api/me/notifications/read-all", "", master, true, http.StatusNoContent},
		{"settings user", http.MethodGet, "/api/admin/settings", "", user, false, http.StatusForbidden},
		{"settings admin", http.MethodGet, "/api/admin/settings", "", admin, false, http.StatusOK},
		{"auto approve user", http.MethodGet, "/api/admin/auto-approve", "", user, false, http.StatusForbidden},
		{"auto approve admin", http.MethodGet, "/api/admin/auto-approve", "", admin, false, http.StatusOK},
		{"auto approve master", http.MethodGet, "/api/admin/auto-approve", "", master, false, http.StatusOK},
		{"auto approve write admin", http.MethodPut, "/api/admin/auto-approve", `{"enabled":true}`, admin, true, http.StatusForbidden},
		{"auto approve write without csrf", http.MethodPut, "/api/admin/auto-approve", `{"enabled":true}`, master, false, http.StatusForbidden},
		{"auto approve write master", http.MethodPut, "/api/admin/auto-approve", `{"enabled":true}`, master, true, http.StatusOK},
		{"mail test user", http.MethodPost, "/api/admin/mail/test", "", user, true, http.StatusForbidden},
		{"mail test admin", http.MethodPost, "/api/admin/mail/test", "", admin, true, http.StatusForbidden},
		{"mail test master without csrf", http.MethodPost, "/api/admin/mail/test", "", master, false, http.StatusForbidden},
		{"mail test master", http.MethodPost, "/api/admin/mail/test", "", master, true, http.StatusOK},
		{"webhook test user", http.MethodPost, "/api/admin/webhook/test", "", user, true, http.StatusForbidden},
		{"webhook test admin", http.MethodPost, "/api/admin/webhook/test", "", admin, true, http.StatusForbidden},
		{"webhook test master without csrf", http.MethodPost, "/api/admin/webhook/test", "", master, false, http.StatusForbidden},
		{"webhook test master", http.MethodPost, "/api/admin/webhook/test", "", master, true, http.StatusOK},
		{"announcement user", http.MethodGet, "/api/announcement", "", user, false, http.StatusOK},
		{"announcement admin", http.MethodGet, "/api/announcement", "", admin, false, http.StatusOK},
		{"announcement write admin", http.MethodPut, "/api/admin/announcement", `{"text":"公告"}`, admin, true, http.StatusForbidden},
		{"announcement write master", http.MethodPut, "/api/admin/announcement", `{"text":"公告"}`, master, true, http.StatusOK},
		{"audit user", http.MethodGet, "/api/audit-logs", "", user, false, http.StatusForbidden},
		{"audit admin", http.MethodGet, "/api/audit-logs", "", admin, false, http.StatusOK},
		{"audit master", http.MethodGet, "/api/audit-logs", "", master, false, http.StatusOK},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			session := tc.session
			csrf := ""
			if tc.csrf {
				csrf = session.CSRFToken
			}
			rec := serve(engine, requestWithSession(tc.method, tc.path, tc.body, csrf, &session))
			if rec.Code != tc.status {
				t.Fatalf("%s %s = %d, want %d; body=%s", tc.method, tc.path, rec.Code, tc.status, rec.Body.String())
			}
		})
	}
}

// 两端点都未装配时，每个端点仍是 501：拆包不得让某一路由静默消失。
func TestSettingsRoutesNilHandlersReturn501(t *testing.T) {
	issuer := newStubIssuer()
	gin.SetMode(gin.TestMode)
	engine, err := router.New(router.Options{Sessions: issuer})
	if err != nil {
		t.Fatalf("router.New: %v", err)
	}
	master := issueUser(t, issuer, auth.RoleMaster)

	cases := []struct {
		method string
		path   string
		body   string
		csrf   bool
	}{
		{http.MethodGet, "/api/admin/notify", "", false},
		{http.MethodPut, "/api/admin/notify", `{"items":[]}`, true},
		{http.MethodGet, "/api/me/notifications", "", false},
		{http.MethodGet, "/api/me/notifications/unread-count", "", false},
		{http.MethodPost, "/api/me/notifications/read", `{"ids":["018f4a4e-0000-7000-8000-000000000001"]}`, true},
		{http.MethodPost, "/api/me/notifications/read-all", "", true},
		{http.MethodGet, "/api/admin/settings", "", false},
		{http.MethodGet, "/api/admin/auto-approve", "", false},
		{http.MethodPut, "/api/admin/auto-approve", `{"enabled":true}`, true},
		{http.MethodPost, "/api/admin/mail/test", "", true},
		{http.MethodPost, "/api/admin/webhook/test", "", true},
		{http.MethodGet, "/api/announcement", "", false},
		{http.MethodPut, "/api/admin/announcement", `{"text":"x"}`, true},
	}
	for _, tc := range cases {
		t.Run(tc.method+" "+tc.path, func(t *testing.T) {
			csrf := ""
			if tc.csrf {
				csrf = master.CSRFToken
			}
			rec := serve(engine, requestWithSession(tc.method, tc.path, tc.body, csrf, &master))
			if rec.Code != http.StatusNotImplemented {
				t.Fatalf("%s %s = %d, want 501; body=%s", tc.method, tc.path, rec.Code, rec.Body.String())
			}
		})
	}
}

type settingsEndpoint struct {
	method string
	path   string
	body   string
	csrf   bool
	status int
}

// 单侧装配：只装通知面时系统面 4 条路径仍回 501，只装系统面时通知面 6 条仍回 501。
// 双 nil 只证明两边都缺时回 501；两组单侧用例钉住每个 wrapper 读的是自己的字段，
// 把 s.notify 误写成 s.system（或反之）必须变红。
func TestSettingsRoutesSingleSideWiring(t *testing.T) {
	notifyCases := []settingsEndpoint{
		{http.MethodGet, "/api/admin/notify", "", false, http.StatusOK},
		{http.MethodPut, "/api/admin/notify", `{"items":[]}`, true, http.StatusOK},
		{http.MethodGet, "/api/me/notifications", "", false, http.StatusOK},
		{http.MethodGet, "/api/me/notifications/unread-count", "", false, http.StatusOK},
		{http.MethodPost, "/api/me/notifications/read", `{"ids":["018f4a4e-0000-7000-8000-000000000001"]}`, true, http.StatusNoContent},
		{http.MethodPost, "/api/me/notifications/read-all", "", true, http.StatusNoContent},
	}
	systemCases := []settingsEndpoint{
		{http.MethodGet, "/api/admin/settings", "", false, http.StatusOK},
		{http.MethodPost, "/api/admin/mail/test", "", true, http.StatusOK},
		{http.MethodPost, "/api/admin/webhook/test", "", true, http.StatusOK},
		{http.MethodGet, "/api/announcement", "", false, http.StatusOK},
		{http.MethodPut, "/api/admin/announcement", `{"text":"公告"}`, true, http.StatusOK},
	}

	run := func(t *testing.T, engine *gin.Engine, session auth.Session, tc settingsEndpoint) {
		t.Helper()
		csrf := ""
		if tc.csrf {
			csrf = session.CSRFToken
		}
		rec := serve(engine, requestWithSession(tc.method, tc.path, tc.body, csrf, &session))
		if rec.Code != tc.status {
			t.Fatalf("%s %s = %d, want %d; body=%s", tc.method, tc.path, rec.Code, tc.status, rec.Body.String())
		}
	}

	t.Run("notify only", func(t *testing.T) {
		notifyHandler := notifyhttp.NewHandler(stubNotify{}).WithInbox(stubInbox{})
		engine, issuer := newSettingsEngineWith(t, notifyHandler, nil)
		master := issueUser(t, issuer, auth.RoleMaster)
		for _, tc := range notifyCases {
			run(t, engine, master, tc)
		}
		for _, tc := range systemCases {
			tc.status = http.StatusNotImplemented
			run(t, engine, master, tc)
		}
	})

	t.Run("system only", func(t *testing.T) {
		systemHandler := systemhttp.NewHandler(stubRoster{}, &stubAnnouncement{}, stubMailTest{}).
			WithWebhookTest(stubWebhookTest{})
		engine, issuer := newSettingsEngineWith(t, nil, systemHandler)
		master := issueUser(t, issuer, auth.RoleMaster)
		for _, tc := range notifyCases {
			tc.status = http.StatusNotImplemented
			run(t, engine, master, tc)
		}
		for _, tc := range systemCases {
			run(t, engine, master, tc)
		}
	})
}
