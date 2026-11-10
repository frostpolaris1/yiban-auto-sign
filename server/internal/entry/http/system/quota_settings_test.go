package system_test

// 配额设置端点（GET/PUT /api/admin/quota-settings，r4r）的入口层守卫。
//
// 判据：GET master/admin 可读、user 403；PUT 只有 master，admin/user 403；
// 部分补丁（缺字段=不动、null=删行回缺省、值=upsert）；负例
// （越域/负数/类型错/未知字段/空体）400 且零写入零审计；
// PUT 后 GET 报 source=db。真路由与 CSRF 由 router 包的 e2e 钉；
// 本文件用例层全真（真解析器＋真写回用例，只替 DB 与审计）。

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	appsettings "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/settings"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	systemhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/system"
)

// quotaSettingsFixture 构造全真配额用例链：解析器＋写回用例，
// DB 与审计用内存替身。
type quotaSettingsFixture struct {
	handler *systemhttp.Handler
	store   *memScheduleStore
	audits  *memAudit
}

func newQuotaSettingsFixture(t *testing.T) *quotaSettingsFixture {
	t.Helper()
	store := newMemScheduleStore()
	resolver, err := appsettings.NewResolver(store, map[string]appsettings.SettingDomain{
		appsettings.KeyScheduleEdgeFrontSec: {Min: 0, Max: 300},
		appsettings.KeyScheduleEdgeBackSec:  {Min: 0, Max: 300},
	}, nil)
	if err != nil {
		t.Fatalf("NewResolver: %v", err)
	}
	audits := &memAudit{}
	svc, err := appsettings.NewService(appsettings.Deps{
		Store: &memAnnouncementStore{}, Audit: audits, Tx: passthroughTx{}, Clock: fixedClock{},
		Schedule: store, Resolver: resolver,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	handler := systemhttp.NewHandler(nil, nil, nil).WithQuotaSettings(svc)
	return &quotaSettingsFixture{handler: handler, store: store, audits: audits}
}

// quotaReportBody 是 GET/PUT 响应的解析形状。
type quotaReportBody struct {
	MaxUsers struct {
		Value  int    `json:"value"`
		Source string `json:"source"`
	} `json:"max_users"`
	MaxAccounts struct {
		Value  int    `json:"value"`
		Source string `json:"source"`
	} `json:"max_accounts"`
}

func (f *quotaSettingsFixture) get(t *testing.T, role coreauth.Role) (*httptest.ResponseRecorder, quotaReportBody) {
	t.Helper()
	c, rec := newContextWithEmail(http.MethodGet, "/api/admin/quota-settings", "", role, "m@example.com")
	f.handler.AdminQuotaSettings(c)
	var body quotaReportBody
	if rec.Code == http.StatusOK {
		if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
			t.Fatalf("decode quota report: %v; body=%q", err, rec.Body.String())
		}
	}
	return rec, body
}

func (f *quotaSettingsFixture) put(t *testing.T, body string, role coreauth.Role) *httptest.ResponseRecorder {
	t.Helper()
	c, rec := newContextWithEmail(http.MethodPut, "/api/admin/quota-settings", body, role, "m@example.com")
	f.handler.AdminQuotaSettingsSet(c, oapi.AdminQuotaSettingsSetParams{})
	return rec
}

// GET：缺行报 1/1 与 source=default；master 与 admin 都可读，user 403。
func TestQuotaSettingsGetDefaultsAndRoles(t *testing.T) {
	f := newQuotaSettingsFixture(t)
	for _, role := range []coreauth.Role{coreauth.RoleMaster, coreauth.RoleAdmin} {
		rec, body := f.get(t, role)
		if rec.Code != http.StatusOK {
			t.Fatalf("%s GET status = %d: %s", role, rec.Code, rec.Body.String())
		}
		if body.MaxUsers.Value != 1 || body.MaxUsers.Source != "default" ||
			body.MaxAccounts.Value != 1 || body.MaxAccounts.Source != "default" {
			t.Fatalf("%s report = %+v, want 1/1 default", role, body)
		}
	}
	rec, _ := f.get(t, coreauth.RoleUser)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("user GET status = %d, want 403", rec.Code)
	}
	if code := errorCode(t, rec); code != "forbidden" {
		t.Fatalf("user GET code = %q, want forbidden", code)
	}
}

// 未注入用例时两端点 501：写面缺失必须显性。
func TestQuotaSettingsUnwiredIs501(t *testing.T) {
	handler := systemhttp.NewHandler(nil, nil, nil)
	c, rec := newContextWithEmail(http.MethodGet, "/api/admin/quota-settings", "", coreauth.RoleMaster, "m@example.com")
	handler.AdminQuotaSettings(c)
	if rec.Code != http.StatusNotImplemented {
		t.Fatalf("GET unwired status = %d, want 501", rec.Code)
	}
	c, rec = newContextWithEmail(http.MethodPut, "/api/admin/quota-settings", `{"max_users":5}`, coreauth.RoleMaster, "m@example.com")
	handler.AdminQuotaSettingsSet(c, oapi.AdminQuotaSettingsSetParams{})
	if rec.Code != http.StatusNotImplemented {
		t.Fatalf("PUT unwired status = %d, want 501", rec.Code)
	}
}

// PUT 成功：200 回生效报告，覆盖行落库，恰一行写回审计；GET 报 source=db。
func TestQuotaSettingsPutRoundTrip(t *testing.T) {
	f := newQuotaSettingsFixture(t)
	rec := f.put(t, `{"max_users":5}`, coreauth.RoleMaster)
	if rec.Code != http.StatusOK {
		t.Fatalf("put status = %d: %s", rec.Code, rec.Body.String())
	}
	var body quotaReportBody
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode put body: %v; body=%q", err, rec.Body.String())
	}
	if body.MaxUsers.Value != 5 || body.MaxUsers.Source != "db" {
		t.Fatalf("put report = %+v, want max_users db 5", body)
	}
	if body.MaxAccounts.Value != 1 || body.MaxAccounts.Source != "default" {
		t.Fatalf("untouched key = %+v, want default 1", body.MaxAccounts)
	}
	if f.store.intRows[appsettings.KeyQuotaMaxUsers] != "5" {
		t.Fatalf("store rows = %v", f.store.intRows)
	}
	if len(f.audits.entries) != 1 || f.audits.entries[0].Action != coreaudit.ActionSettingsQuotaWriteback {
		t.Fatalf("audit entries = %+v, want one settings.quota_writeback", f.audits.entries)
	}

	_, got := f.get(t, coreauth.RoleAdmin)
	if got.MaxUsers.Value != 5 || got.MaxUsers.Source != "db" {
		t.Fatalf("GET after PUT = %+v, want db 5", got.MaxUsers)
	}
}

// 0=不限是合法写值；显式 null 删行回缺省。
func TestQuotaSettingsPutZeroAndClear(t *testing.T) {
	f := newQuotaSettingsFixture(t)
	if rec := f.put(t, `{"max_accounts":0}`, coreauth.RoleMaster); rec.Code != http.StatusOK {
		t.Fatalf("zero put status = %d: %s", rec.Code, rec.Body.String())
	}
	_, body := f.get(t, coreauth.RoleMaster)
	if body.MaxAccounts.Value != 0 || body.MaxAccounts.Source != "db" {
		t.Fatalf("zero report = %+v, want db 0 (unlimited)", body.MaxAccounts)
	}
	if rec := f.put(t, `{"max_accounts":null}`, coreauth.RoleMaster); rec.Code != http.StatusOK {
		t.Fatalf("clear put status = %d: %s", rec.Code, rec.Body.String())
	}
	_, body = f.get(t, coreauth.RoleMaster)
	if body.MaxAccounts.Value != 1 || body.MaxAccounts.Source != "default" {
		t.Fatalf("cleared report = %+v, want default 1", body.MaxAccounts)
	}
	pair := f.audits.entries[1].Detail["changes"].(map[string][2]any)[appsettings.KeyQuotaMaxAccounts]
	if pair[0] != 0 || pair[1] != nil {
		t.Fatalf("clear pair = %#v, want [0 nil]", pair)
	}
}

// 负例：越域、负数、类型错、未知字段、空体一律 400 且零写入零审计。
func TestQuotaSettingsPutRejectsInvalidBodies(t *testing.T) {
	cases := []struct{ name, body string }{
		{"negative", `{"max_users":-1}`},
		{"above max", `{"max_users":100001}`},
		{"wrong type", `{"max_users":"5"}`},
		{"float", `{"max_users":1.5}`},
		{"unknown field", `{"max_things":5}`},
		{"empty", `{}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newQuotaSettingsFixture(t)
			rec := f.put(t, tc.body, coreauth.RoleMaster)
			if rec.Code != http.StatusBadRequest {
				t.Fatalf("status = %d, want 400; body=%q", rec.Code, rec.Body.String())
			}
			if code := errorCode(t, rec); code != "validation_failed" {
				t.Fatalf("code = %q, want validation_failed", code)
			}
			if len(f.store.intRows) != 0 || f.store.intWrites != 0 || f.store.clears != 0 {
				t.Fatalf("被拒写到达存储: rows=%v writes=%d clears=%d",
					f.store.intRows, f.store.intWrites, f.store.clears)
			}
			if len(f.audits.entries) != 0 {
				t.Fatalf("被拒写写了 %d 行审计", len(f.audits.entries))
			}
		})
	}
}

// PUT 只有 master：admin 与 user 都 403 零写入零审计。
func TestQuotaSettingsPutIsMasterOnly(t *testing.T) {
	f := newQuotaSettingsFixture(t)
	for _, role := range []coreauth.Role{coreauth.RoleAdmin, coreauth.RoleUser} {
		rec := f.put(t, `{"max_users":5}`, role)
		if rec.Code != http.StatusForbidden {
			t.Fatalf("%s PUT status = %d, want 403; body=%q", role, rec.Code, rec.Body.String())
		}
	}
	if len(f.store.intRows) != 0 || len(f.audits.entries) != 0 {
		t.Fatalf("被拒写有副作用: rows=%v audit=%d", f.store.intRows, len(f.audits.entries))
	}
}

// 配额键不进名册：覆盖行存在时 GET /api/admin/settings 仍不出现 quota.*。
func TestQuotaKeysAreNotInRoster(t *testing.T) {
	store := newMemScheduleStore()
	resolver, err := appsettings.NewResolver(store, map[string]appsettings.SettingDomain{
		appsettings.KeyScheduleEdgeFrontSec: {Min: 0, Max: 300},
		appsettings.KeyScheduleEdgeBackSec:  {Min: 0, Max: 300},
	}, nil)
	if err != nil {
		t.Fatalf("NewResolver: %v", err)
	}
	roster, err := appsettings.NewRoster([]appsettings.Entry{
		{Key: "YIBAN_WINDOW_EDGE_FRONT_SEC", Group: "调度", Default: float64(60), HasDefault: true},
	}, mapSource{})
	if err != nil {
		t.Fatalf("NewRoster: %v", err)
	}
	roster.WithOverrides(resolver)
	audits := &memAudit{}
	svc, err := appsettings.NewService(appsettings.Deps{
		Store: &memAnnouncementStore{}, Audit: audits, Tx: passthroughTx{}, Clock: fixedClock{},
		Schedule: store, Resolver: resolver,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	handler := systemhttp.NewHandler(roster, nil, nil).WithQuotaSettings(svc)
	c, rec := newContextWithEmail(http.MethodPut, "/api/admin/quota-settings", `{"max_users":5}`, coreauth.RoleMaster, "m@example.com")
	handler.AdminQuotaSettingsSet(c, oapi.AdminQuotaSettingsSetParams{})
	if rec.Code != http.StatusOK {
		t.Fatalf("put status = %d: %s", rec.Code, rec.Body.String())
	}
	c, rec = newContextWithEmail(http.MethodGet, "/api/admin/settings", "", coreauth.RoleAdmin, "a@example.com")
	handler.AdminSettings(c)
	if rec.Code != http.StatusOK {
		t.Fatalf("GET settings status = %d: %s", rec.Code, rec.Body.String())
	}
	if strings.Contains(rec.Body.String(), "quota.") {
		t.Fatalf("名册泄漏了配额键: %s", rec.Body.String())
	}
}
