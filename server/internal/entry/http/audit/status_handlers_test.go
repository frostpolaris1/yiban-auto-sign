package audit_test

// 审计链巡检端点的 HTTP 门与响应映射。
//
// 口径：未登录 401；user 403；master/admin 200。
// 断链仍是 200：巡检结论不是 HTTP 错误。
// 响应只带结论与定位行 id，不带审计正文。

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"
	"time"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	audithttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/audit"
)

type fakeStatusReader struct {
	status appaudit.ChainStatus
	err    error
	called int
}

func (f *fakeStatusReader) Status(context.Context) (appaudit.ChainStatus, error) {
	f.called++
	return f.status, f.err
}

func statusHandler(reader *fakeStatusReader) *audithttp.Handler {
	return audithttp.NewHandler(&fakeReader{}).WithStatus(reader)
}

func TestAuditStatusRoleGate(t *testing.T) {
	handler := statusHandler(&fakeStatusReader{status: appaudit.ChainStatus{OK: true}})

	c, rec := newContext(http.MethodGet, "/api/admin/audit-status", "")
	handler.AuditStatus(c)
	if rec.Code != http.StatusUnauthorized || errorCode(t, rec) != "unauthorized" {
		t.Fatalf("anonymous = %d %s", rec.Code, rec.Body.String())
	}

	c, rec = newContext(http.MethodGet, "/api/admin/audit-status", coreauth.RoleUser)
	handler.AuditStatus(c)
	if rec.Code != http.StatusForbidden || errorCode(t, rec) != "forbidden" {
		t.Fatalf("user = %d %s", rec.Code, rec.Body.String())
	}

	for _, role := range []coreauth.Role{coreauth.RoleAdmin, coreauth.RoleMaster} {
		c, rec = newContext(http.MethodGet, "/api/admin/audit-status", role)
		handler.AuditStatus(c)
		if rec.Code != http.StatusOK {
			t.Fatalf("%s = %d %s", role, rec.Code, rec.Body.String())
		}
	}
}

func TestAuditStatusReportsBrokenChainAsConclusion(t *testing.T) {
	broken := int64(7)
	at := time.Date(2026, 11, 8, 9, 15, 30, 0, time.UTC)
	reader := &fakeStatusReader{status: appaudit.ChainStatus{
		OK:          false,
		CheckedRows: 7,
		LastHash:    "ab",
		BrokenAt:    &broken,
		CheckedAt:   at,
	}}
	handler := statusHandler(reader)

	c, rec := newContext(http.MethodGet, "/api/admin/audit-status", coreauth.RoleMaster)
	handler.AuditStatus(c)
	if rec.Code != http.StatusOK {
		t.Fatalf("broken chain = %d %s", rec.Code, rec.Body.String())
	}

	var body struct {
		OK          bool      `json:"ok"`
		Empty       bool      `json:"empty"`
		CheckedRows int64     `json:"checked_rows"`
		LastHash    string    `json:"last_hash"`
		BrokenAt    *int64    `json:"broken_at"`
		CheckedAt   time.Time `json:"checked_at"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v; body=%s", err, rec.Body.String())
	}
	if body.OK || body.Empty {
		t.Fatalf("body = %+v", body)
	}
	if body.CheckedRows != 7 || body.LastHash != "ab" {
		t.Fatalf("body = %+v", body)
	}
	if body.BrokenAt == nil || *body.BrokenAt != 7 {
		t.Fatalf("broken_at = %v, want 7", body.BrokenAt)
	}
	if !body.CheckedAt.Equal(at) {
		t.Fatalf("checked_at = %v, want %v", body.CheckedAt, at)
	}
	if reader.called != 1 {
		t.Fatalf("status reader called %d times, want 1", reader.called)
	}
}
