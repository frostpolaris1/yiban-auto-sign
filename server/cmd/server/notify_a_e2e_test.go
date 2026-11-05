package main

// 站内信读端的 HTTP e2e：真 PostgreSQL + 真路由 + 真会话。
//
// 覆盖：匿名 401、user 403、master/admin 200；键集翻页无重无漏；
// 非法 cursor/limit 400；越权 id 静默忽略；read-all 幂等；未读数只算本人。
// 不经过 assembly：本文件只装配 settings 处理器与真实 inbox 用例。

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/session"
	appnotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/notify"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/router"
	settingshttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/settings"
)

type notifyHTTPEnv struct {
	t        *testing.T
	ctx      context.Context
	adminDB  *pg.DB
	db       *pg.DB
	engine   *gin.Engine
	sessions *session.Memory
	repo     *pg.NotificationRepo
}

func newNotifyHTTPEnv(t *testing.T) *notifyHTTPEnv {
	t.Helper()
	gin.SetMode(gin.TestMode)
	base := w2cRequireDSN(t)
	ctx := t.Context()
	schema := "notify_a_" + w2cRandomHex(6)

	adminDB, err := pg.Open(ctx, pg.Options{DSN: base, MaxConns: 1, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open admin connection: %v", err)
	}
	if _, err := adminDB.Exec(ctx, "CREATE SCHEMA "+schema); err != nil {
		adminDB.Close()
		t.Fatalf("create schema %s: %v", schema, err)
	}
	dsn := w2cWithSchema(base, schema)
	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 4, StatementTimeout: 10 * time.Second})
	if err != nil {
		w2cDropSchema(adminDB, schema)
		adminDB.Close()
		t.Fatalf("open test connection: %v", err)
	}
	if _, err := pg.Migrate(ctx, dsn); err != nil {
		db.Close()
		w2cDropSchema(adminDB, schema)
		adminDB.Close()
		t.Fatalf("migrate test schema: %v", err)
	}

	clk := clock.System{}
	sessions := session.NewMemoryWithTTL(clk, 30*time.Minute, 12*time.Hour,
		[]byte("notify-a-session-secret-0123456789abc"))
	repo := pg.NewNotificationRepo(db)
	inboxSvc, err := appnotify.NewInbox(appnotify.InboxDeps{Repo: repo, Clock: clk})
	if err != nil {
		t.Fatalf("new inbox: %v", err)
	}
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	engine, err := router.New(router.Options{
		Sessions:        sessions,
		Clock:           clk,
		Logger:          logger,
		InsecureCookies: true,
		Settings:        settingshttp.NewHandler(nil, nil, nil, nil).WithInbox(inboxSvc),
	})
	if err != nil {
		t.Fatalf("router.New: %v", err)
	}
	t.Cleanup(func() {
		db.Close()
		w2cDropSchema(adminDB, schema)
		adminDB.Close()
	})
	return &notifyHTTPEnv{
		t: t, ctx: ctx, adminDB: adminDB, db: db, engine: engine, sessions: sessions, repo: repo,
	}
}

func (e *notifyHTTPEnv) user(email string, role coreauth.Role) coreuser.Record {
	e.t.Helper()
	users := pg.NewUserRepo(e.db)
	var rec coreuser.Record
	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		var err error
		rec, err = users.Create(ctx, coreuser.New{
			Email: email, PasswordHash: "argon2id$test", Role: role,
		}, 10000)
		return err
	})
	if err != nil {
		e.t.Fatalf("create user %s: %v", email, err)
	}
	return rec
}

func (e *notifyHTTPEnv) login(user coreuser.Record) *coreauth.Issued {
	e.t.Helper()
	issued, err := e.sessions.Issue(e.ctx, coreauth.User{
		ID: user.ID, Email: user.Email, Role: user.Role, Status: coreauth.StatusActive,
	})
	if err != nil {
		e.t.Fatalf("issue session: %v", err)
	}
	return &issued
}

func (e *notifyHTTPEnv) seed(userID, dedupe string, at time.Time) corenotify.Notification {
	e.t.Helper()
	item, err := e.repo.Insert(e.ctx, corenotify.NotificationInsert{
		UserID: userID, Category: corenotify.CategoryReviewPending,
		DedupeKey: dedupe, Title: "通知", Body: "正文", CreatedAt: at,
	})
	if err != nil {
		e.t.Fatalf("seed notification %s: %v", dedupe, err)
	}
	return item
}

func (e *notifyHTTPEnv) do(method, path string, body any, issued *coreauth.Issued) *httptest.ResponseRecorder {
	e.t.Helper()
	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			e.t.Fatalf("marshal body: %v", err)
		}
		reader = bytes.NewReader(raw)
	}
	req := httptest.NewRequest(method, path, reader)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if issued != nil {
		req.AddCookie(&http.Cookie{Name: coreauth.InsecureCookieName, Value: issued.Token})
		req.Header.Set("X-CSRF-Token", issued.CSRFToken)
	}
	rec := httptest.NewRecorder()
	e.engine.ServeHTTP(rec, req)
	return rec
}

func (e *notifyHTTPEnv) page(rec *httptest.ResponseRecorder) oapi.NotificationPage {
	e.t.Helper()
	var page oapi.NotificationPage
	if err := json.Unmarshal(rec.Body.Bytes(), &page); err != nil {
		e.t.Fatalf("decode page (%d): %v; body=%q", rec.Code, err, rec.Body.String())
	}
	return page
}

func (e *notifyHTTPEnv) unread(issued *coreauth.Issued) int {
	e.t.Helper()
	rec := e.do(http.MethodGet, "/api/me/notifications/unread-count", nil, issued)
	if rec.Code != http.StatusOK {
		e.t.Fatalf("unread status = %d %s", rec.Code, rec.Body.String())
	}
	var out oapi.UnreadCount
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		e.t.Fatalf("decode unread: %v", err)
	}
	return out.Count
}

func errorResponseCode(t *testing.T, rec *httptest.ResponseRecorder) string {
	t.Helper()
	var envelope struct {
		Error struct {
			Code string `json:"code"`
		} `json:"error"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &envelope); err != nil {
		t.Fatalf("decode error (%d): %v; body=%q", rec.Code, err, rec.Body.String())
	}
	return envelope.Error.Code
}

// 匿名 401、user 403、master/admin 200。
func TestNotificationHTTPRoleGate(t *testing.T) {
	e := newNotifyHTTPEnv(t)
	admin := e.login(e.user("http-admin@example.com", coreauth.RoleAdmin))
	user := e.login(e.user("http-user@example.com", coreauth.RoleUser))
	master := e.login(e.user("http-master@example.com", coreauth.RoleMaster))
	e.seed(e.user("http-owner@example.com", coreauth.RoleAdmin).ID, "http/1", clock.System{}.Now().UTC())

	rec := e.do(http.MethodGet, "/api/me/notifications", nil, nil)
	if rec.Code != http.StatusUnauthorized || errorResponseCode(t, rec) != "unauthorized" {
		t.Fatalf("anonymous = %d %s", rec.Code, rec.Body.String())
	}
	rec = e.do(http.MethodGet, "/api/me/notifications", nil, user)
	if rec.Code != http.StatusForbidden || errorResponseCode(t, rec) != "forbidden" {
		t.Fatalf("user list = %d %s", rec.Code, rec.Body.String())
	}
	rec = e.do(http.MethodGet, "/api/me/notifications/unread-count", nil, user)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("user count = %d %s", rec.Code, rec.Body.String())
	}
	for _, issued := range []*coreauth.Issued{admin, master} {
		if rec := e.do(http.MethodGet, "/api/me/notifications", nil, issued); rec.Code != http.StatusOK {
			t.Fatalf("list = %d %s", rec.Code, rec.Body.String())
		}
		if rec := e.do(http.MethodGet, "/api/me/notifications/unread-count", nil, issued); rec.Code != http.StatusOK {
			t.Fatalf("count = %d %s", rec.Code, rec.Body.String())
		}
	}
}

// 真路由的键集翻页：两页无重无漏，末页无 next_cursor；非法 cursor/limit 400。
func TestNotificationHTTPPagination(t *testing.T) {
	e := newNotifyHTTPEnv(t)
	owner := e.user("page-owner@example.com", coreauth.RoleAdmin)
	issued := e.login(owner)
	base := clock.System{}.Now().UTC().Add(-time.Hour)
	for i := 0; i < 3; i++ {
		e.seed(owner.ID, "page/"+string(rune('a'+i)), base.Add(time.Duration(i)*time.Minute))
	}

	rec := e.do(http.MethodGet, "/api/me/notifications?limit=2", nil, issued)
	if rec.Code != http.StatusOK {
		t.Fatalf("page 1 = %d %s", rec.Code, rec.Body.String())
	}
	first := e.page(rec)
	if len(first.Items) != 2 || first.NextCursor == nil || *first.NextCursor == "" {
		t.Fatalf("page 1 = %+v", first)
	}
	rec = e.do(http.MethodGet, "/api/me/notifications?limit=2&cursor="+*first.NextCursor, nil, issued)
	if rec.Code != http.StatusOK {
		t.Fatalf("page 2 = %d %s", rec.Code, rec.Body.String())
	}
	second := e.page(rec)
	if len(second.Items) != 1 || second.NextCursor != nil {
		t.Fatalf("page 2 = %+v", second)
	}
	seen := map[string]bool{}
	for _, item := range append(first.Items, second.Items...) {
		if seen[item.Id] {
			t.Fatalf("duplicate row %s", item.Id)
		}
		seen[item.Id] = true
	}
	if len(seen) != 3 {
		t.Fatalf("rows = %d, want 3", len(seen))
	}

	for _, path := range []string{
		"/api/me/notifications?cursor=not-base64",
		"/api/me/notifications?limit=0",
		"/api/me/notifications?limit=101",
	} {
		rec := e.do(http.MethodGet, path, nil, issued)
		if rec.Code != http.StatusBadRequest || errorResponseCode(t, rec) != "validation_failed" {
			t.Fatalf("%s = %d %s", path, rec.Code, rec.Body.String())
		}
	}
}

// 越权 id 静默忽略；read-all 幂等；未读数只算本人。
func TestNotificationHTTPReadOwnership(t *testing.T) {
	e := newNotifyHTTPEnv(t)
	alice := e.user("http-alice@example.com", coreauth.RoleAdmin)
	bob := e.user("http-bob@example.com", coreauth.RoleMaster)
	aliceIssued := e.login(alice)
	bobIssued := e.login(bob)
	at := clock.System{}.Now().UTC().Add(-time.Hour)
	aliceOne := e.seed(alice.ID, "read/alice-1", at)
	e.seed(alice.ID, "read/alice-2", at.Add(time.Minute))
	bobOne := e.seed(bob.ID, "read/bob-1", at)

	if got := e.unread(aliceIssued); got != 2 {
		t.Fatalf("alice unread = %d, want 2", got)
	}
	if got := e.unread(bobIssued); got != 1 {
		t.Fatalf("bob unread = %d, want 1", got)
	}

	rec := e.do(http.MethodPost, "/api/me/notifications/read", map[string]any{
		"ids": []string{aliceOne.ID, bobOne.ID, "018f4a4e-0000-7000-8000-00000000ffff"},
	}, aliceIssued)
	if rec.Code != http.StatusNoContent {
		t.Fatalf("mark read = %d %s", rec.Code, rec.Body.String())
	}
	if got := e.unread(aliceIssued); got != 1 {
		t.Fatalf("alice unread after mark = %d, want 1", got)
	}
	if got := e.unread(bobIssued); got != 1 {
		t.Fatalf("bob unread changed to %d", got)
	}

	for i := 0; i < 2; i++ {
		rec := e.do(http.MethodPost, "/api/me/notifications/read-all", nil, aliceIssued)
		if rec.Code != http.StatusNoContent {
			t.Fatalf("read-all #%d = %d %s", i+1, rec.Code, rec.Body.String())
		}
	}
	if got := e.unread(aliceIssued); got != 0 {
		t.Fatalf("alice unread after read-all = %d", got)
	}
	if got := e.unread(bobIssued); got != 1 {
		t.Fatalf("bob unread after alice read-all = %d, want 1", got)
	}

	// 已读后再标记同一 id 仍 204（幂等）。
	rec = e.do(http.MethodPost, "/api/me/notifications/read",
		map[string]any{"ids": []string{aliceOne.ID}}, aliceIssued)
	if rec.Code != http.StatusNoContent {
		t.Fatalf("repeat mark = %d %s", rec.Code, rec.Body.String())
	}
}
