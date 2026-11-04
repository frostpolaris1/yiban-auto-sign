package router_test

// 设置面四个端点的接线门。
//
// 断言：路由确实挂上了 settings/audit 处理器；未登录 401、user 403、
// master/admin 按契约放行；写端点缺 CSRF 令牌 403。
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
	audithttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/router"
	settingshttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/settings"
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

type stubRoster struct{}

func (stubRoster) Report() appsettings.Report { return appsettings.Report{} }

type stubAnnouncement struct{ text string }

func (s stubAnnouncement) Get(context.Context) (appsettings.Announcement, error) {
	return appsettings.Announcement{Text: s.text}, nil
}

func (s stubAnnouncement) Set(_ context.Context, text string, _ appaudit.Actor) (appsettings.Announcement, error) {
	s.text = text
	return appsettings.Announcement{Text: text}, nil
}

type stubAuditReader struct{}

func (stubAuditReader) List(context.Context, appaudit.ListFilter) (appaudit.LogPage, error) {
	return appaudit.LogPage{}, nil
}

func newSettingsEngine(t *testing.T) (*gin.Engine, *stubIssuer) {
	t.Helper()
	issuer := newStubIssuer()
	gin.SetMode(gin.TestMode)
	engine, err := router.New(router.Options{
		Sessions: issuer,
		Settings: settingshttp.NewHandler(stubNotify{}, stubRoster{}, stubAnnouncement{}),
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

	// 未登录：四个端点都 401（announcement 读同样要求会话）。
	for _, path := range []string{
		"/api/admin/notify", "/api/admin/settings", "/api/announcement", "/api/audit-logs",
	} {
		rec := serve(engine, requestWithSession(http.MethodGet, path, "", "", nil))
		if rec.Code != http.StatusUnauthorized {
			t.Fatalf("%s anonymous = %d", path, rec.Code)
		}
	}

	user := issueUser(t, issuer, auth.RoleUser)
	admin := issueUser(t, issuer, auth.RoleAdmin)
	master := issueUser(t, issuer, auth.RoleMaster)

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
		{"settings user", http.MethodGet, "/api/admin/settings", "", user, false, http.StatusForbidden},
		{"settings admin", http.MethodGet, "/api/admin/settings", "", admin, false, http.StatusOK},
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
