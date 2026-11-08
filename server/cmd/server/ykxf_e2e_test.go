package main

// ykxf e2e：平台探测、学校投影、自动审批开关与异步单步核验。
//
// # 功能
// 用真实 PostgreSQL 与 w2c 的假 provider 钉住：
// detect 首次建校、二次命中、disabled 视同未命中、并发建校 409 回落；
// create + 开关开 + confirm_defaults → 自动 active + 审计 + 管理员通知；
// 身份不匹配 / 开关关 / 未带 confirm_defaults → 留 pending。
//
// # 归属
// cmd/server 可同时引用 adapters 与 entry；internal/entry 不能，故放这里。
//
// # 口径
// 凭据零落库：detect 前后 accounts/credentials 行数不变，响应与日志
// 不含手机号、口令或设备码本体。等待后台核验用有界轮询，不用固定睡眠
// 判成功。

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	appschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/school"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

type ykxfDetectResult struct {
	UniversityName string `json:"university_name"`
	UniversityID   string `json:"university_id"`
	School         *struct {
		ID   string `json:"id"`
		Name string `json:"name"`
	} `json:"school"`
	PersonName        string `json:"person_name"`
	DeviceModel       string `json:"device_model"`
	DeviceCodePresent bool   `json:"device_code_present"`
}

func ykxfEventually(t *testing.T, what string, fn func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if fn() {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

func ykxfSetSwitch(t *testing.T, e *w2cEnv, sess *coreauth.Issued, enabled bool) {
	t.Helper()
	rec := e.do(http.MethodPut, "/api/admin/auto-approve", map[string]any{"enabled": enabled}, sess)
	w2cWantStatus(t, rec, http.StatusOK)
	var status struct {
		Enabled bool `json:"enabled"`
	}
	e.decode(rec, &status)
	if status.Enabled != enabled {
		t.Fatalf("switch = %v, want %v", status.Enabled, enabled)
	}
}

func ykxfSchoolWithPlatform(t *testing.T, e *w2cEnv, code, universityID string) appschool.Record {
	t.Helper()
	record, err := e.schools.Create(e.ctx, appschool.New{
		Code: code, Name: "School " + code, PlatformUniversityID: universityID,
	})
	if err != nil {
		t.Fatalf("create school %s: %v", code, err)
	}
	return record
}

func TestYkxfDetectCreatesSchoolThenReusesIt(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("master@example.com", coreauth.RoleMaster)
	sess := e.login(master)
	const (
		phone    = "13800138061"
		password = "platform-secret"
		device   = "DEVICE-SECRET-61"
	)
	e.provider.profile = contract.Profile{
		DisplayName: "测*生", SchoolName: "示例大学", UniversityID: "0000000061",
	}
	e.device.set(appaccount.DeviceState{Authorized: true, Code: device, PhoneModel: "Mi 9"}, nil)

	before := e.scalarInt("SELECT count(*) FROM accounts")
	rec := e.do(http.MethodPost, "/api/accounts/detect",
		map[string]any{"phone": phone, "password": password}, sess)
	w2cWantStatus(t, rec, http.StatusOK)
	var first ykxfDetectResult
	e.decode(rec, &first)
	if first.UniversityName != "示例大学" || first.UniversityID != "0000000061" {
		t.Fatalf("identity = %#v", first)
	}
	if first.School == nil || first.School.ID == "" || first.School.Name != "示例大学" {
		t.Fatalf("school = %#v", first.School)
	}
	if first.PersonName != "测*生" || first.DeviceModel != "Mi 9" || !first.DeviceCodePresent {
		t.Fatalf("result = %#v", first)
	}
	for _, secret := range []string{phone, password, device} {
		if strings.Contains(rec.Body.String(), secret) {
			t.Fatalf("detect response leaked %q: %s", secret, rec.Body.String())
		}
	}
	if got := e.scalarInt("SELECT count(*) FROM accounts"); got != before {
		t.Fatalf("detect changed account rows: %d -> %d", before, got)
	}
	if got := e.scalarInt("SELECT count(*) FROM credentials"); got != 0 {
		t.Fatalf("detect wrote %d credentials, want 0", got)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM schools WHERE platform_university_id = '0000000061' AND status = 'active'"); got != 1 {
		t.Fatalf("school rows = %d, want 1", got)
	}
	if got := e.scalarString("SELECT name || '|' || code FROM schools WHERE platform_university_id = '0000000061'"); !strings.HasPrefix(got, "示例大学|auto-") {
		t.Fatalf("school name/code = %q", got)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM audit_logs WHERE action = 'school.auto_create' AND detail = '{\"university_id\":\"0000000061\"}'"); got != 1 {
		t.Fatalf("school.auto_create audit rows = %d, want 1", got)
	}
	if got := e.scalarString("SELECT actor_label FROM audit_logs WHERE action = 'school.auto_create'"); got != "auto-detect" {
		t.Fatalf("auto create actor = %q, want auto-detect", got)
	}

	// 二次探测命中同一校，不重复建校、不重复审计。
	rec = e.do(http.MethodPost, "/api/accounts/detect",
		map[string]any{"phone": phone, "password": password}, sess)
	w2cWantStatus(t, rec, http.StatusOK)
	var second ykxfDetectResult
	e.decode(rec, &second)
	if second.School == nil || second.School.ID != first.School.ID {
		t.Fatalf("second detect school = %#v, want %s", second.School, first.School.ID)
	}
	if got := e.scalarInt("SELECT count(*) FROM schools WHERE platform_university_id = '0000000061'"); got != 1 {
		t.Fatalf("second detect left %d school rows, want 1", got)
	}
	if got := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'school.auto_create'"); got != 1 {
		t.Fatalf("second detect wrote %d auto create audits, want 1", got)
	}

	// 日志正文也不含凭据。
	if strings.Contains(e.logs.String(), phone) || strings.Contains(e.logs.String(), password) ||
		strings.Contains(e.logs.String(), device) {
		t.Fatalf("logs leaked detect credentials: %s", e.logs.String())
	}
}

func TestYkxfDetectDisabledSchoolIsTreatedAsAbsent(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("master@example.com", coreauth.RoleMaster)
	school := ykxfSchoolWithPlatform(t, e, "ykxf-dis", "0000000062")
	if _, err := e.db.Exec(e.ctx, "UPDATE schools SET status = 'disabled' WHERE id = $1::uuid", school.ID); err != nil {
		t.Fatalf("disable school: %v", err)
	}
	e.provider.profile = contract.Profile{
		DisplayName: "测*生", SchoolName: "停用大学", UniversityID: "0000000062",
	}
	e.device.set(appaccount.DeviceState{Authorized: true, Code: "CODE-62"}, nil)

	rec := e.do(http.MethodPost, "/api/accounts/detect",
		map[string]any{"phone": "13800138062", "password": "secret"}, e.login(master))
	w2cWantStatus(t, rec, http.StatusOK)
	var result ykxfDetectResult
	e.decode(rec, &result)
	if result.School != nil {
		t.Fatalf("disabled school = %#v, want null", result.School)
	}
	if got := e.scalarInt("SELECT count(*) FROM schools WHERE platform_university_id = '0000000062'"); got != 1 {
		t.Fatalf("disabled hit left %d schools, want the original 1", got)
	}
	if got := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'school.auto_create'"); got != 0 {
		t.Fatalf("disabled hit wrote %d auto create audits, want 0", got)
	}
}

func TestYkxfDetectConcurrentCreatesOneSchool(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("master@example.com", coreauth.RoleMaster)
	sess := e.login(master)
	e.provider.profile = contract.Profile{
		DisplayName: "测*生", SchoolName: "并发大学", UniversityID: "0000000063",
	}
	e.device.set(appaccount.DeviceState{Authorized: true, Code: "CODE-63"}, nil)

	body, err := json.Marshal(map[string]any{"phone": "13800138063", "password": "secret"})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var wg sync.WaitGroup
	codes := make([]int, 2)
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func(at int) {
			defer wg.Done()
			req := httptest.NewRequest(http.MethodPost, "/api/accounts/detect", bytes.NewReader(body))
			req.Header.Set("Content-Type", "application/json")
			req.AddCookie(&http.Cookie{Name: coreauth.InsecureCookieName, Value: sess.Token})
			req.Header.Set("X-CSRF-Token", sess.CSRFToken)
			rec := httptest.NewRecorder()
			e.engine.ServeHTTP(rec, req)
			codes[at] = rec.Code
		}(i)
	}
	wg.Wait()
	for i, code := range codes {
		if code != http.StatusOK {
			t.Fatalf("concurrent detect %d = %d, want 200", i, code)
		}
	}
	if got := e.scalarInt("SELECT count(*) FROM schools WHERE platform_university_id = '0000000063'"); got != 1 {
		t.Fatalf("concurrent detect left %d schools, want 1", got)
	}
	if got := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'school.auto_create'"); got != 1 {
		t.Fatalf("concurrent detect wrote %d auto create audits, want 1", got)
	}
}

func TestYkxfAutoApproveOnMatchingIdentity(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("master@example.com", coreauth.RoleMaster)
	admin := e.user("admin@example.com", coreauth.RoleAdmin)
	school := ykxfSchoolWithPlatform(t, e, "ykxf-ok", "0000000064")
	e.bind(admin.ID, school.ID)
	masterSess := e.login(master)

	// 默认 false；admin 可读，写入必须 master。
	rec := e.do(http.MethodGet, "/api/admin/auto-approve", nil, e.login(admin))
	w2cWantStatus(t, rec, http.StatusOK)
	var status struct {
		Enabled bool `json:"enabled"`
	}
	e.decode(rec, &status)
	if status.Enabled {
		t.Fatal("a fresh deployment reports auto approve enabled")
	}
	w2cWantStatus(t, e.do(http.MethodPut, "/api/admin/auto-approve",
		map[string]any{"enabled": true}, e.login(admin)), http.StatusForbidden)
	ykxfSetSwitch(t, e, masterSess, true)
	if got := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'settings.auto_approve'"); got != 1 {
		t.Fatalf("switch audit rows = %d, want 1", got)
	}

	e.provider.profile = contract.Profile{UniversityID: "0000000064"}
	rec = e.do(http.MethodPost, "/api/accounts", map[string]any{
		"school_id":        school.ID,
		"phone":            "13800138064",
		"password":         "stored-secret",
		"confirm_defaults": true,
	}, masterSess)
	w2cWantStatus(t, rec, http.StatusCreated)
	var created w2cAccount
	e.decode(rec, &created)
	if created.Status != "pending" {
		t.Fatalf("create status = %q, want pending", created.Status)
	}

	ykxfEventually(t, "auto approval", func() bool {
		return e.scalarString("SELECT status::text FROM accounts WHERE id = $1::uuid", created.ID) == "active"
	})
	if got := e.scalarInt(
		"SELECT count(*) FROM audit_logs WHERE action = 'account.auto_approve' AND target_id = $1", created.ID); got != 1 {
		t.Fatalf("auto approve audit rows = %d, want 1", got)
	}
	detail := e.scalarString(
		"SELECT detail FROM audit_logs WHERE action = 'account.auto_approve' AND target_id = $1", created.ID)
	if detail != `{"field":"status","from":"pending","to":"active"}` {
		t.Fatalf("auto approve detail = %s", detail)
	}
	if got := e.scalarString(
		"SELECT actor_label || '|' || coalesce(actor_user_id::text, '') || '|' || coalesce(actor_role::text, '') FROM audit_logs WHERE action = 'account.auto_approve'"); got != "auto-approve||" {
		t.Fatalf("auto approve actor = %q", got)
	}
	ykxfEventually(t, "administrator notification", func() bool {
		return e.scalarInt(
			"SELECT count(*) FROM notifications WHERE category = 'review_pending' AND school_id = $1::uuid AND title LIKE '%自动%'",
			school.ID) >= 1
	})
}

func TestYkxfAutoApproveLeavesPendingOnMismatch(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("master@example.com", coreauth.RoleMaster)
	school := ykxfSchoolWithPlatform(t, e, "ykxf-mis", "0000000065")
	masterSess := e.login(master)
	ykxfSetSwitch(t, e, masterSess, true)
	e.provider.profile = contract.Profile{UniversityID: "0000009999"}

	rec := e.do(http.MethodPost, "/api/accounts", map[string]any{
		"school_id":        school.ID,
		"phone":            "13800138065",
		"password":         "stored-secret",
		"confirm_defaults": true,
	}, masterSess)
	w2cWantStatus(t, rec, http.StatusCreated)
	var created w2cAccount
	e.decode(rec, &created)

	ykxfEventually(t, "failed auto check audit", func() bool {
		return e.scalarInt(
			"SELECT count(*) FROM audit_logs WHERE action = 'account.auto_check_failed' AND target_id = $1", created.ID) == 1
	})
	if status := e.scalarString("SELECT status::text FROM accounts WHERE id = $1::uuid", created.ID); status != "pending" {
		t.Fatalf("status = %q, want pending", status)
	}
	detail := e.scalarString(
		"SELECT detail FROM audit_logs WHERE action = 'account.auto_check_failed' AND target_id = $1", created.ID)
	if detail != `{"result":"identity_mismatch"}` {
		t.Fatalf("failed detail = %s, want only the result word", detail)
	}
	if strings.Contains(detail, "9999") || strings.Contains(detail, "0065") {
		t.Fatalf("failed detail leaked a value: %s", detail)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM audit_logs WHERE action = 'account.auto_approve' AND target_id = $1", created.ID); got != 0 {
		t.Fatalf("mismatch wrote %d auto approve audits", got)
	}
}

func TestYkxfAutoApproveOffStaysPending(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("master@example.com", coreauth.RoleMaster)
	school := ykxfSchoolWithPlatform(t, e, "ykxf-off", "0000000066")
	masterSess := e.login(master)
	e.provider.profile = contract.Profile{UniversityID: "0000000066"}

	rec := e.do(http.MethodPost, "/api/accounts", map[string]any{
		"school_id":        school.ID,
		"phone":            "13800138066",
		"password":         "stored-secret",
		"confirm_defaults": true,
	}, masterSess)
	w2cWantStatus(t, rec, http.StatusCreated)
	var created w2cAccount
	e.decode(rec, &created)

	time.Sleep(500 * time.Millisecond)
	if status := e.scalarString("SELECT status::text FROM accounts WHERE id = $1::uuid", created.ID); status != "pending" {
		t.Fatalf("status = %q, want pending while the switch is off", status)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM audit_logs WHERE action IN ('account.auto_approve', 'account.auto_check_failed') AND target_id = $1",
		created.ID); got != 0 {
		t.Fatalf("switch off wrote %d auto check audits", got)
	}
}

func TestYkxfAutoApproveSkippedWithoutConfirmDefaults(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("master@example.com", coreauth.RoleMaster)
	school := ykxfSchoolWithPlatform(t, e, "ykxf-custom", "0000000067")
	masterSess := e.login(master)
	ykxfSetSwitch(t, e, masterSess, true)
	e.provider.profile = contract.Profile{UniversityID: "0000000067"}

	// 用户改过机型：没有 confirm_defaults，自动核验必须跳过。
	rec := e.do(http.MethodPost, "/api/accounts", map[string]any{
		"school_id":   school.ID,
		"phone":       "13800138067",
		"password":    "stored-secret",
		"phone_model": "Custom Phone",
	}, masterSess)
	w2cWantStatus(t, rec, http.StatusCreated)
	var created w2cAccount
	e.decode(rec, &created)

	time.Sleep(500 * time.Millisecond)
	if status := e.scalarString("SELECT status::text FROM accounts WHERE id = $1::uuid", created.ID); status != "pending" {
		t.Fatalf("status = %q, want pending without confirm_defaults", status)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM audit_logs WHERE action IN ('account.auto_approve', 'account.auto_check_failed') AND target_id = $1",
		created.ID); got != 0 {
		t.Fatalf("missing confirm_defaults wrote %d auto check audits", got)
	}
}

func TestYkxfDetectRateLimitedPerPhone(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("master@example.com", coreauth.RoleMaster)
	sess := e.login(master)
	e.provider.profile = contract.Profile{
		DisplayName: "测*生", SchoolName: "限流大学", UniversityID: "0000000068",
	}
	e.device.set(appaccount.DeviceState{Authorized: true, Code: "CODE-68"}, nil)

	for i := 0; i < 5; i++ {
		rec := e.do(http.MethodPost, "/api/accounts/detect",
			map[string]any{"phone": "13800138068", "password": "secret"}, sess)
		w2cWantStatus(t, rec, http.StatusOK)
	}
	rec := e.do(http.MethodPost, "/api/accounts/detect",
		map[string]any{"phone": "13800138068", "password": "secret"}, sess)
	w2cWantStatus(t, rec, http.StatusTooManyRequests)
	if rec.Header().Get("Retry-After") == "" {
		t.Fatal("429 is missing Retry-After")
	}
}
