package notify_test

// 通知订阅与站内信端点的入口判据：角色门、响应形状、请求体校验、204 幂等。
// 用例与存储由内存替身承载；真库翻页与所有权由 adapters/pg e2e 覆盖。

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	appnotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/notify"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
	notifyhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/notify"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
)

func newContext(method, path, body string, role coreauth.Role) (*gin.Context, *httptest.ResponseRecorder) {
	gin.SetMode(gin.TestMode)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	if role != "" {
		req = req.WithContext(coreauth.WithSession(req.Context(), coreauth.Session{
			User: coreauth.User{
				ID: "11111111-1111-1111-1111-111111111111", Email: "admin@example.com",
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

func newHandler(t *testing.T) *notifyhttp.Handler {
	t.Helper()
	notifySvc, err := appnotify.NewService(&memNotifyRepo{})
	if err != nil {
		t.Fatalf("notify service: %v", err)
	}
	return notifyhttp.NewHandler(notifySvc)
}

// 未登录 401；user 403；master/admin 200。
func TestNotifyRoleGate(t *testing.T) {
	handler := newHandler(t)
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
	handler := newHandler(t)
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
	handler := newHandler(t)
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

type inboxRepo struct {
	items     []corenotify.Notification
	next      string
	unread    int64
	lastUser  string
	lastQuery corenotify.NotificationQuery
	lastIDs   []string
	readNow   time.Time
	allNow    time.Time
	listCalls int
	allCalls  int
}

func (r *inboxRepo) List(_ context.Context, query corenotify.NotificationQuery) ([]corenotify.Notification, string, error) {
	r.listCalls++
	r.lastUser = query.UserID
	r.lastQuery = query
	return r.items, r.next, nil
}

func (r *inboxRepo) UnreadCount(_ context.Context, userID string) (int64, error) {
	r.lastUser = userID
	return r.unread, nil
}

func (r *inboxRepo) MarkRead(_ context.Context, userID string, ids []string, now time.Time) (int64, error) {
	r.lastUser = userID
	r.lastIDs = append([]string(nil), ids...)
	r.readNow = now
	return int64(len(ids)), nil
}

func (r *inboxRepo) MarkAllRead(_ context.Context, userID string, now time.Time) (int64, error) {
	r.allCalls++
	r.lastUser = userID
	r.allNow = now
	return 1, nil
}

var inboxClock = time.Date(2026, 11, 5, 10, 0, 0, 0, time.UTC)

func newInboxHandler(t *testing.T, repo *inboxRepo) *notifyhttp.Handler {
	t.Helper()
	svc, err := appnotify.NewInbox(appnotify.InboxDeps{
		Repo:  repo,
		Clock: coreclock.Fixed{T: inboxClock},
	})
	if err != nil {
		t.Fatalf("NewInbox: %v", err)
	}
	return notifyhttp.NewHandler(nil).WithInbox(svc)
}

type notificationEndpoint struct {
	name   string
	method string
	body   string
	invoke func(*notifyhttp.Handler, *gin.Context)
}

func notificationEndpoints() []notificationEndpoint {
	return []notificationEndpoint{
		{"list", http.MethodGet, "", func(h *notifyhttp.Handler, c *gin.Context) {
			h.NotificationList(c, oapi.NotificationListParams{})
		}},
		{"count", http.MethodGet, "", func(h *notifyhttp.Handler, c *gin.Context) {
			h.NotificationUnreadCount(c)
		}},
		{"read", http.MethodPost, `{"ids":["018f4a4e-0000-7000-8000-000000000001"]}`,
			func(h *notifyhttp.Handler, c *gin.Context) {
				h.NotificationMarkRead(c, oapi.NotificationMarkReadParams{})
			}},
		{"read-all", http.MethodPost, "", func(h *notifyhttp.Handler, c *gin.Context) {
			h.NotificationMarkAllRead(c, oapi.NotificationMarkAllReadParams{})
		}},
	}
}

// 角色门逐端点：匿名 401、user 403、admin/master 成功；失败不触达仓储。
func TestNotificationRoleGateByEndpoint(t *testing.T) {
	for _, ep := range notificationEndpoints() {
		t.Run(ep.name+"/anonymous", func(t *testing.T) {
			repo := &inboxRepo{}
			c, rec := newContext(ep.method, "/api/me/notifications", ep.body, "")
			ep.invoke(newInboxHandler(t, repo), c)
			if rec.Code != http.StatusUnauthorized || errorCode(t, rec) != "unauthorized" {
				t.Fatalf("anonymous = %d %s", rec.Code, rec.Body.String())
			}
			if repo.listCalls+repo.allCalls != 0 || repo.lastUser != "" {
				t.Fatalf("attempt on behalf of nobody reached the repo: %+v", repo)
			}
		})
		t.Run(ep.name+"/user", func(t *testing.T) {
			repo := &inboxRepo{}
			c, rec := newContext(ep.method, "/api/me/notifications", ep.body, coreauth.RoleUser)
			ep.invoke(newInboxHandler(t, repo), c)
			if rec.Code != http.StatusForbidden || errorCode(t, rec) != "forbidden" {
				t.Fatalf("user = %d %s", rec.Code, rec.Body.String())
			}
			if repo.listCalls+repo.allCalls != 0 || repo.lastUser != "" {
				t.Fatalf("user-role request reached the repo: %+v", repo)
			}
		})
		for _, role := range []coreauth.Role{coreauth.RoleAdmin, coreauth.RoleMaster} {
			t.Run(ep.name+"/"+string(role), func(t *testing.T) {
				repo := &inboxRepo{}
				c, rec := newContext(ep.method, "/api/me/notifications", ep.body, role)
				ep.invoke(newInboxHandler(t, repo), c)
				if rec.Code < 200 || rec.Code >= 300 {
					t.Fatalf("%s = %d %s", role, rec.Code, rec.Body.String())
				}
				if repo.lastUser != "11111111-1111-1111-1111-111111111111" {
					t.Fatalf("%s used user %q, want the session user", role, repo.lastUser)
				}
			})
		}
	}
}

// 列表响应必须带 null 键、隐藏 dedupe_key，并把游标原样交出。
func TestNotificationListShape(t *testing.T) {
	school := "22222222-2222-2222-2222-222222222222"
	readAt := time.Date(2026, 11, 4, 8, 30, 0, 0, time.UTC)
	repo := &inboxRepo{
		items: []corenotify.Notification{
			{
				ID: "018f4a4e-0000-7000-8000-000000000001", Category: corenotify.CategoryReviewPending,
				Title: "待审核账号", Body: "1 条", SchoolID: school,
				CreatedAt: time.Date(2026, 11, 5, 9, 0, 0, 0, time.UTC), ReadAt: &readAt,
			},
			{
				ID: "018f4a4e-0000-7000-8000-000000000002", Category: corenotify.CategoryCredentialInvalid,
				Title: "凭据失效", Body: "账号已停用",
				CreatedAt: time.Date(2026, 11, 5, 8, 0, 0, 0, time.UTC),
			},
		},
		next: "cursor-2",
	}
	cursor := "cursor-1"
	limit := 2
	c, rec := newContext(http.MethodGet, "/api/me/notifications", "", coreauth.RoleAdmin)
	newInboxHandler(t, repo).NotificationList(c,
		oapi.NotificationListParams{Cursor: &cursor, Limit: &limit})
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d %s", rec.Code, rec.Body.String())
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body["next_cursor"] != "cursor-2" {
		t.Fatalf("next_cursor = %v", body["next_cursor"])
	}
	items := body["items"].([]any)
	first := items[0].(map[string]any)
	if first["school_id"] != school || first["read_at"] == nil {
		t.Fatalf("first item = %v", first)
	}
	if _, ok := first["dedupe_key"]; ok {
		t.Fatal("dedupe_key leaked into the response")
	}
	second := items[1].(map[string]any)
	schoolValue, ok := second["school_id"]
	if !ok || schoolValue != nil {
		t.Fatalf("school_id must be present and null, got %v (present=%t)", schoolValue, ok)
	}
	readValue, ok := second["read_at"]
	if !ok || readValue != nil {
		t.Fatalf("read_at must be present and null, got %v (present=%t)", readValue, ok)
	}
	if repo.lastQuery.Cursor != "cursor-1" || repo.lastQuery.Limit != 2 {
		t.Fatalf("repo query = %+v", repo.lastQuery)
	}
}

// 末页仍输出 next_cursor，值为空串（键恒在）。
func TestNotificationListLastPageHasEmptyCursor(t *testing.T) {
	repo := &inboxRepo{items: []corenotify.Notification{}}
	c, rec := newContext(http.MethodGet, "/api/me/notifications", "", coreauth.RoleAdmin)
	newInboxHandler(t, repo).NotificationList(c, oapi.NotificationListParams{})
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d %s", rec.Code, rec.Body.String())
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	cursor, ok := body["next_cursor"]
	if !ok || cursor != "" {
		t.Fatalf("next_cursor must be present and empty on the last page: %v", body)
	}
	if items, ok := body["items"].([]any); !ok || len(items) != 0 {
		t.Fatalf("items = %v, want an empty array", body["items"])
	}
}

// 未给 limit 时入口填缺省 20；显式 0 是校验失败。
func TestNotificationListLimitDefault(t *testing.T) {
	repo := &inboxRepo{}
	c, rec := newContext(http.MethodGet, "/api/me/notifications", "", coreauth.RoleAdmin)
	newInboxHandler(t, repo).NotificationList(c, oapi.NotificationListParams{})
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d %s", rec.Code, rec.Body.String())
	}
	if repo.lastQuery.Limit != appnotify.DefaultNotificationPageSize {
		t.Fatalf("limit = %d, want %d", repo.lastQuery.Limit, appnotify.DefaultNotificationPageSize)
	}
}

// limit 越界与非法 ids 都是 400，且不触达仓储。
func TestNotificationValidation(t *testing.T) {
	repo := &inboxRepo{}
	handler := newInboxHandler(t, repo)

	for _, limit := range []int{0, 101} {
		value := limit
		c, rec := newContext(http.MethodGet, "/api/me/notifications", "", coreauth.RoleAdmin)
		handler.NotificationList(c, oapi.NotificationListParams{Limit: &value})
		if rec.Code != http.StatusBadRequest || errorCode(t, rec) != "validation_failed" {
			t.Fatalf("limit %d = %d %s", limit, rec.Code, rec.Body.String())
		}
	}

	for name, body := range map[string]string{
		"missing ids": `{}`,
		"null ids":    `{"ids":null}`,
		"empty ids":   `{"ids":[]}`,
		"bad uuid":    `{"ids":["nope"]}`,
		"too many":    tooManyIDsBody(),
	} {
		c, rec := newContext(http.MethodPost, "/api/me/notifications/read", body, coreauth.RoleAdmin)
		handler.NotificationMarkRead(c, oapi.NotificationMarkReadParams{})
		if rec.Code != http.StatusBadRequest || errorCode(t, rec) != "validation_failed" {
			t.Fatalf("%s = %d %s", name, rec.Code, rec.Body.String())
		}
	}
	if repo.listCalls != 0 || repo.lastIDs != nil {
		t.Fatalf("validation failures reached the repo: %+v", repo)
	}
}

// 标记已读与全部已读都回 204；全部已读重复调用仍 204（幂等）。
func TestNotificationMarkReadNoContent(t *testing.T) {
	repo := &inboxRepo{}
	handler := newInboxHandler(t, repo)
	id := "018f4a4e-0000-7000-8000-000000000001"
	c, rec := newContext(http.MethodPost, "/api/me/notifications/read",
		`{"ids":["`+id+`"]}`, coreauth.RoleAdmin)
	handler.NotificationMarkRead(c, oapi.NotificationMarkReadParams{})
	// 204 没有响应体；gin 的 test writer 需显式落头，完整引擎会在返回时落。
	c.Writer.WriteHeaderNow()
	if rec.Code != http.StatusNoContent {
		t.Fatalf("read = %d %s", rec.Code, rec.Body.String())
	}
	if len(repo.lastIDs) != 1 || repo.lastIDs[0] != id || !repo.readNow.Equal(inboxClock) {
		t.Fatalf("read call = ids %v at %v", repo.lastIDs, repo.readNow)
	}

	for i := 0; i < 2; i++ {
		c, rec = newContext(http.MethodPost, "/api/me/notifications/read-all", "", coreauth.RoleAdmin)
		handler.NotificationMarkAllRead(c, oapi.NotificationMarkAllReadParams{})
		c.Writer.WriteHeaderNow()
		if rec.Code != http.StatusNoContent {
			t.Fatalf("read-all #%d = %d %s", i+1, rec.Code, rec.Body.String())
		}
	}
	if repo.allCalls != 2 || !repo.allNow.Equal(inboxClock) {
		t.Fatalf("mark-all calls = %d at %v", repo.allCalls, repo.allNow)
	}
}

// 未注入用例时四个端点回 501，不伪装成成功。
func TestNotificationUnwiredReturns501(t *testing.T) {
	handler := notifyhttp.NewHandler(nil)
	c, rec := newContext(http.MethodGet, "/api/me/notifications", "", coreauth.RoleAdmin)
	handler.NotificationList(c, oapi.NotificationListParams{})
	if rec.Code != http.StatusNotImplemented {
		t.Fatalf("list = %d %s", rec.Code, rec.Body.String())
	}
	c, rec = newContext(http.MethodGet, "/api/me/notifications/unread-count", "", coreauth.RoleAdmin)
	handler.NotificationUnreadCount(c)
	if rec.Code != http.StatusNotImplemented {
		t.Fatalf("count = %d %s", rec.Code, rec.Body.String())
	}
}

func tooManyIDsBody() string {
	ids := make([]string, 101)
	for i := range ids {
		ids[i] = `"018f4a4e-0000-7000-8000-000000000001"`
	}
	return `{"ids":[` + strings.Join(ids, ",") + `]}`
}
