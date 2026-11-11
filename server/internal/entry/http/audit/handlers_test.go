package audit_test

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	audithttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
)

type fakeReader struct {
	page   appaudit.LogPage
	filter appaudit.ListFilter
	called int
}

func (f *fakeReader) List(_ context.Context, filter appaudit.ListFilter) (appaudit.LogPage, error) {
	f.called++
	f.filter = filter
	return f.page, nil
}

func newContext(method, path string, role coreauth.Role) (*gin.Context, *httptest.ResponseRecorder) {
	gin.SetMode(gin.TestMode)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	req := httptest.NewRequest(method, path, nil)
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

// 未登录 401；user 403；master/admin 200。
func TestAuditLogListRoleGate(t *testing.T) {
	handler := audithttp.NewHandler(&fakeReader{})
	c, rec := newContext(http.MethodGet, "/api/audit-logs", "")
	handler.AuditLogList(c, oapi.AuditLogListParams{})
	if rec.Code != http.StatusUnauthorized || errorCode(t, rec) != "unauthorized" {
		t.Fatalf("anonymous = %d %s", rec.Code, rec.Body.String())
	}

	c, rec = newContext(http.MethodGet, "/api/audit-logs", coreauth.RoleUser)
	handler.AuditLogList(c, oapi.AuditLogListParams{})
	if rec.Code != http.StatusForbidden || errorCode(t, rec) != "forbidden" {
		t.Fatalf("user = %d %s", rec.Code, rec.Body.String())
	}

	for _, role := range []coreauth.Role{coreauth.RoleAdmin, coreauth.RoleMaster} {
		c, rec = newContext(http.MethodGet, "/api/audit-logs", role)
		handler.AuditLogList(c, oapi.AuditLogListParams{})
		if rec.Code != http.StatusOK {
			t.Fatalf("%s = %d %s", role, rec.Code, rec.Body.String())
		}
	}
}

// 参数映射：action/actor/target/from/to/cursor/limit 原样进入用例；
// 响应带 target、total 与 has_more。
func TestAuditLogListPassesFilters(t *testing.T) {
	reader := &fakeReader{page: appaudit.LogPage{
		Items: []appaudit.LogRow{{
			ID: 7, TS: time.Date(2026, 11, 4, 8, 0, 0, 0, time.UTC),
			Action: "auth.login", ActorLabel: "a***@example.com",
			Target: "account-1", Detail: "{}",
		}},
		Total:      42,
		NextCursor: "next",
	}}
	handler := audithttp.NewHandler(reader)
	c, rec := newContext(http.MethodGet, "/api/audit-logs", coreauth.RoleMaster)
	limit := 10
	action, actor, target, cursor := "auth.login", "alice@example.com", "account-1", "opaque"
	from := time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC)
	to := from.Add(24 * time.Hour)
	handler.AuditLogList(c, oapi.AuditLogListParams{
		Action: &action, Actor: &actor, Target: &target, Cursor: &cursor, From: &from, To: &to, Limit: &limit,
	})
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d body=%s", rec.Code, rec.Body.String())
	}
	if reader.filter.Action != action || reader.filter.Actor != actor ||
		reader.filter.Target != target || reader.filter.Cursor != cursor || reader.filter.Limit != limit {
		t.Fatalf("filter = %+v", reader.filter)
	}
	if reader.filter.From == nil || !reader.filter.From.Equal(from) ||
		reader.filter.To == nil || !reader.filter.To.Equal(to) {
		t.Fatalf("bounds = %v/%v", reader.filter.From, reader.filter.To)
	}
	var body struct {
		Items []struct {
			ID     int64  `json:"id"`
			Action string `json:"action"`
			Actor  string `json:"actor"`
			Target string `json:"target"`
			Detail string `json:"detail"`
			At     string `json:"at"`
		} `json:"items"`
		NextCursor string `json:"next_cursor"`
		Total      int64  `json:"total"`
		HasMore    bool   `json:"has_more"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode page: %v", err)
	}
	if len(body.Items) != 1 || body.Items[0].ID != 7 || body.Items[0].Actor != "a***@example.com" ||
		body.Items[0].Target != "account-1" || body.NextCursor != "next" {
		t.Fatalf("body = %s", rec.Body.String())
	}
	if body.Total != 42 || !body.HasMore {
		t.Fatalf("total/has_more = %d/%v, want 42/true", body.Total, body.HasMore)
	}
}

// 分页边界：显式 0 与 201 都 400，且不读仓储。
func TestAuditLogListLimitBoundary(t *testing.T) {
	for _, limit := range []int{0, 201} {
		reader := &fakeReader{}
		handler := audithttp.NewHandler(reader)
		c, rec := newContext(http.MethodGet, "/api/audit-logs", coreauth.RoleAdmin)
		value := limit
		handler.AuditLogList(c, oapi.AuditLogListParams{Limit: &value})
		if rec.Code != http.StatusBadRequest || errorCode(t, rec) != "validation_failed" {
			t.Fatalf("limit %d = %d %s", limit, rec.Code, rec.Body.String())
		}
		if reader.called != 0 {
			t.Fatalf("limit %d reached the use case", limit)
		}
	}
	// 缺省与上界内的值进入用例。
	reader := &fakeReader{}
	handler := audithttp.NewHandler(reader)
	c, _ := newContext(http.MethodGet, "/api/audit-logs", coreauth.RoleAdmin)
	limit := 200
	handler.AuditLogList(c, oapi.AuditLogListParams{Limit: &limit})
	if reader.filter.Limit != 200 {
		t.Fatalf("limit = %d, want 200", reader.filter.Limit)
	}
}
