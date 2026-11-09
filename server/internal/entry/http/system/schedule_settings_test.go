package system_test

// 调度设置写回端点（PUT /api/admin/schedule-settings，sett2）的入口层守卫。
//
// 判据：角色门（user 403）、部分补丁、显式 null 删行、域校验负例
// （越域/类型错/未知字段/空体→400 且零写入零审计）、PUT 后 GET 名册
// 报 source=db。真路由与 CSRF 由 router 包的 e2e 钉；本文件用例层全真
// （真名册＋真解析器＋真写回用例，只替 DB 与审计）。

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"

	appsettings "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/settings"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	systemhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/system"
)

// memScheduleStore 是入口测试用的内存覆盖行存取。
type memScheduleStore struct {
	intRows    map[string]string
	boolRows   map[string]string
	intWrites  int
	boolWrites int
	clears     int
}

func newMemScheduleStore() *memScheduleStore {
	return &memScheduleStore{intRows: map[string]string{}, boolRows: map[string]string{}}
}

func (m *memScheduleStore) IntOverride(_ context.Context, key string) (int, bool, error) {
	raw, ok := m.intRows[key]
	if !ok {
		return 0, false, nil
	}
	value, err := strconv.Atoi(raw)
	if err != nil {
		return 0, true, errs.New(errs.CodeValidation, "settings value is not an integer")
	}
	return value, true, nil
}

func (m *memScheduleStore) BoolOverride(_ context.Context, key string) (bool, bool, error) {
	raw, ok := m.boolRows[key]
	if !ok {
		return false, false, nil
	}
	switch raw {
	case "true":
		return true, true, nil
	case "false":
		return false, true, nil
	default:
		return false, true, errs.New(errs.CodeValidation, "settings value is not a boolean literal")
	}
}

func (m *memScheduleStore) SetInt(_ context.Context, key string, value int, _ string) error {
	m.intWrites++
	m.intRows[key] = strconv.Itoa(value)
	return nil
}

func (m *memScheduleStore) SetBool(_ context.Context, key string, value bool, _ string) error {
	m.boolWrites++
	m.boolRows[key] = strconv.FormatBool(value)
	return nil
}

func (m *memScheduleStore) ClearSetting(_ context.Context, key string) error {
	m.clears++
	delete(m.intRows, key)
	delete(m.boolRows, key)
	return nil
}

// scheduleSettingsFixture 构造全真用例链：名册（两键样本）＋解析器＋
// 写回用例，DB 与审计用内存替身。
type scheduleSettingsFixture struct {
	handler *systemhttp.Handler
	store   *memScheduleStore
	audits  *memAudit
}

func newScheduleSettingsFixture(t *testing.T) *scheduleSettingsFixture {
	t.Helper()
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
		{Key: "YIBAN_ALLOW_TIME_PREF", Group: "调度", Default: false, HasDefault: true},
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
	handler := systemhttp.NewHandler(roster, svc, nil).
		WithAutoApprove(svc).
		WithScheduleSettings(svc)
	return &scheduleSettingsFixture{handler: handler, store: store, audits: audits}
}

// putScheduleSettings 以指定角色发一次 PUT，返回响应。
func (f *scheduleSettingsFixture) putScheduleSettings(t *testing.T, body string, role coreauth.Role) *httptest.ResponseRecorder {
	t.Helper()
	c, rec := newContextWithEmail(http.MethodPut, "/api/admin/schedule-settings", body, role, "m@example.com")
	f.handler.AdminScheduleSettingsSet(c, oapi.AdminScheduleSettingsSetParams{})
	return rec
}

// getSettings 以 admin 角色读名册（GET 允许 admin），返回键→{source,value}。
func (f *scheduleSettingsFixture) getSettings(t *testing.T) map[string]map[string]any {
	t.Helper()
	c, rec := newContextWithEmail(http.MethodGet, "/api/admin/settings", "", coreauth.RoleAdmin, "a@example.com")
	f.handler.AdminSettings(c)
	if rec.Code != http.StatusOK {
		t.Fatalf("GET settings status = %d: %s", rec.Code, rec.Body.String())
	}
	var parsed struct {
		Groups []struct {
			Items []struct {
				Key    string `json:"key"`
				Source string `json:"source"`
				Value  any    `json:"value"`
			} `json:"items"`
		} `json:"groups"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &parsed); err != nil {
		t.Fatalf("decode roster: %v", err)
	}
	items := map[string]map[string]any{}
	for _, group := range parsed.Groups {
		for _, item := range group.Items {
			items[item.Key] = map[string]any{"source": item.Source, "value": item.Value}
		}
	}
	return items
}

// PUT 成功：200 回全量名册，覆盖行落库，恰一行写回审计。
func TestScheduleSettingsPutRoundTrip(t *testing.T) {
	f := newScheduleSettingsFixture(t)
	rec := f.putScheduleSettings(t, `{"edge_front_sec":120}`, coreauth.RoleMaster)
	if rec.Code != http.StatusOK {
		t.Fatalf("put status = %d: %s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), `"groups"`) {
		t.Fatalf("put body is not a roster: %s", rec.Body.String())
	}
	if f.store.intWrites != 1 || f.store.intRows[appsettings.KeyScheduleEdgeFrontSec] != "120" {
		t.Fatalf("store rows = %v writes = %d", f.store.intRows, f.store.intWrites)
	}
	if len(f.audits.entries) != 1 {
		t.Fatalf("audit rows = %d, want 1", len(f.audits.entries))
	}

	items := f.getSettings(t)
	edge := items["YIBAN_WINDOW_EDGE_FRONT_SEC"]
	if edge["source"] != "db" || edge["value"] != float64(120) {
		t.Fatalf("roster item = %v, want source=db value=120", edge)
	}

	// 显式 null 删行：source 回 default（env 未设置），行消失。
	rec = f.putScheduleSettings(t, `{"edge_front_sec":null}`, coreauth.RoleMaster)
	if rec.Code != http.StatusOK {
		t.Fatalf("null put status = %d", rec.Code)
	}
	if f.store.clears != 1 {
		t.Fatalf("clears = %d, want 1", f.store.clears)
	}
	items = f.getSettings(t)
	edge = items["YIBAN_WINDOW_EDGE_FRONT_SEC"]
	if edge["source"] != "default" || edge["value"] != float64(60) {
		t.Fatalf("roster after clear = %v, want default 60", edge)
	}
}

// 部分补丁：只动列出的键。
func TestScheduleSettingsPutPartialPatch(t *testing.T) {
	f := newScheduleSettingsFixture(t)
	if rec := f.putScheduleSettings(t, `{"edge_front_sec":120}`, coreauth.RoleMaster); rec.Code != http.StatusOK {
		t.Fatalf("first put status = %d", rec.Code)
	}
	if rec := f.putScheduleSettings(t, `{"allow_time_pref":true}`, coreauth.RoleMaster); rec.Code != http.StatusOK {
		t.Fatalf("second put status = %d", rec.Code)
	}
	if f.store.intRows[appsettings.KeyScheduleEdgeFrontSec] != "120" {
		t.Fatalf("edge row changed by allow_time_pref patch: %v", f.store.intRows)
	}
	if len(f.audits.entries) != 2 {
		t.Fatalf("audit rows = %d, want 2", len(f.audits.entries))
	}
}

// 域校验负例：400 且零写入零审计。
func TestScheduleSettingsPutRejectsInvalidBodies(t *testing.T) {
	f := newScheduleSettingsFixture(t)
	for _, body := range []string{
		`{"edge_front_sec":301}`,
		`{"edge_front_sec":-1}`,
		`{"edge_front_sec":"true"}`,
		`{"edge_front_sec":12.5}`,
		`{"allow_time_pref":"true"}`,
		`{"allow_time_pref":1}`,
		`{"unknown_key":1}`,
		`{}`,
		`not-json`,
	} {
		rec := f.putScheduleSettings(t, body, coreauth.RoleMaster)
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("body %s status = %d, want 400: %s", body, rec.Code, rec.Body.String())
		}
	}
	if f.store.intWrites != 0 || f.store.boolWrites != 0 || f.store.clears != 0 {
		t.Fatalf("被拒体写了存储: %v %v %v", f.store.intRows, f.store.boolRows, f.store.clears)
	}
	if len(f.audits.entries) != 0 {
		t.Fatalf("被拒体写了审计: %d rows", len(f.audits.entries))
	}
}

// 角色负例：user 与 admin 都 403，零写入零审计。
func TestScheduleSettingsPutIsMasterOnly(t *testing.T) {
	f := newScheduleSettingsFixture(t)
	for _, role := range []coreauth.Role{coreauth.RoleUser, coreauth.RoleAdmin} {
		rec := f.putScheduleSettings(t, `{"edge_front_sec":120}`, role)
		if rec.Code != http.StatusForbidden {
			t.Fatalf("role %v status = %d, want 403", role, rec.Code)
		}
	}
	if f.store.intWrites != 0 || len(f.audits.entries) != 0 {
		t.Fatalf("被拒角色触达存储或审计")
	}
}

// ===== 批 3b：四枚 DB-only 键的传输映射与成对校验 =====

// 新字段 PUT 往返：周末门布尔与窗口分钟对落行；null 删行回缺行语义。
func TestScheduleSettingsPutWeekendAndWindowFields(t *testing.T) {
	f := newScheduleSettingsFixture(t)
	ctx := context.Background()

	// 周末门布尔：true 落行。
	if rec := f.putScheduleSettings(t, `{"sunday_sign":true,"saturday_sign":true}`, coreauth.RoleMaster); rec.Code != http.StatusOK {
		t.Fatalf("weekend put = %d: %s", rec.Code, rec.Body.String())
	}
	if f.store.boolRows[appsettings.KeyScheduleSundaySign] != "true" ||
		f.store.boolRows[appsettings.KeyScheduleSaturdaySign] != "true" {
		t.Fatalf("weekend rows = %v", f.store.boolRows)
	}

	// 窗口分钟对：成对写入 480/540。
	if rec := f.putScheduleSettings(t, `{"default_sign_start_min":480,"default_sign_end_min":540}`, coreauth.RoleMaster); rec.Code != http.StatusOK {
		t.Fatalf("window put = %d: %s", rec.Code, rec.Body.String())
	}
	if f.store.intRows[appsettings.KeyScheduleDefaultSignStartMin] != "480" ||
		f.store.intRows[appsettings.KeyScheduleDefaultSignEndMin] != "540" {
		t.Fatalf("window rows = %v", f.store.intRows)
	}

	// 显式 null 删行：删后 BoolOverride 缺行=false（周末门缺行语义）。
	if rec := f.putScheduleSettings(t, `{"sunday_sign":null}`, coreauth.RoleMaster); rec.Code != http.StatusOK {
		t.Fatalf("null put = %d: %s", rec.Code, rec.Body.String())
	}
	if value, found, err := f.store.BoolOverride(ctx, appsettings.KeyScheduleSundaySign); err != nil || found || value {
		t.Fatalf("after null BoolOverride = (%v, %v, %v), want missing = false", value, found, err)
	}
}

// 新字段负例：类型错、越域、单侧组合违规都 400 且零写入零审计；
// 成对合法写入 200。
func TestScheduleSettingsPutRejectsWeekendAndWindowBodies(t *testing.T) {
	for _, body := range []string{
		`{"sunday_sign":1}`,
		`{"sunday_sign":"true"}`,
		`{"default_sign_start_min":1440,"default_sign_end_min":1500}`,
		`{"default_sign_start_min":-1,"default_sign_end_min":500}`,
		`{"default_sign_start_min":"60"}`,
		`{"default_sign_start_min":600}`,
		`{"default_sign_end_min":300}`,
	} {
		f := newScheduleSettingsFixture(t)
		rec := f.putScheduleSettings(t, body, coreauth.RoleMaster)
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("body %s status = %d, want 400: %s", body, rec.Code, rec.Body.String())
		}
		if f.store.intWrites != 0 || f.store.boolWrites != 0 || f.store.clears != 0 {
			t.Fatalf("body %s 写了存储: %v %v %v", body, f.store.intRows, f.store.boolRows, f.store.clears)
		}
		if len(f.audits.entries) != 0 {
			t.Fatalf("body %s 写了审计: %d rows", body, len(f.audits.entries))
		}
	}
	// 成对合法：200 且两行落库。
	f := newScheduleSettingsFixture(t)
	if rec := f.putScheduleSettings(t, `{"default_sign_start_min":600,"default_sign_end_min":650}`, coreauth.RoleMaster); rec.Code != http.StatusOK {
		t.Fatalf("valid pair status = %d: %s", rec.Code, rec.Body.String())
	}
	if f.store.intRows[appsettings.KeyScheduleDefaultSignStartMin] != "600" ||
		f.store.intRows[appsettings.KeyScheduleDefaultSignEndMin] != "650" {
		t.Fatalf("pair rows = %v", f.store.intRows)
	}
}
