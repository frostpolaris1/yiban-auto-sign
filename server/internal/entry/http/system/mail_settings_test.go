package system_test

// SMTP 设置端点（GET/PUT /api/admin/mail-settings，smtp4）的入口层守卫。
//
// 判据：GET master/admin 可读、user 403；PUT 只有 master，admin/user 403；
// 任一字段变更都要求 current_password（缺失 400、错误 403，均零写入）；
// 部分补丁（缺字段=不动、null=删行、值=upsert）；负例 400 零写入；
// 响应不回显 pass 明文/密文，from/user 只给固定掩码。
// 真路由与 CSRF 由 router 包的 e2e 钉；用例层全真由 app/settings 测试钉。

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"testing"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appsettings "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/settings"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	systemhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/system"
)

// stubMailSettings 记录写意图；报告固定为 env 值。
type stubMailSettings struct {
	sets    int
	changes []appsettings.MailChange
}

func (s *stubMailSettings) MailSettings(context.Context, corescope.Scope) (appsettings.MailReport, error) {
	return appsettings.MailReport{
		Host:    appsettings.MailValue{Value: "smtp.env.example", Source: appsettings.MailSourceEnv},
		Port:    appsettings.MailPortValue{Value: 465, Source: appsettings.MailSourceEnv},
		TLSMode: appsettings.MailValue{Value: "auto", Source: appsettings.MailSourceEnv},
		From:    appsettings.MailSensitiveValue{Set: true, Masked: "****", Source: appsettings.MailSourceEnv},
		User:    appsettings.MailSensitiveValue{Set: true, Masked: "****", Source: appsettings.MailSourceEnv},
		Pass:    appsettings.MailSecretValue{Set: true, Source: appsettings.MailSourceEnv},
	}, nil
}

func (s *stubMailSettings) SetMail(_ context.Context, _ corescope.Scope, _ appaudit.Actor,
	changes []appsettings.MailChange) error {
	s.sets++
	s.changes = changes
	return nil
}

// stubReauth 记录二次口令调用。
type stubReauth struct {
	calls    int
	password string
	err      error
}

func (r *stubReauth) Reauthenticate(_ context.Context, _ string, password string) error {
	r.calls++
	r.password = password
	return r.err
}

func newMailSettingsHandler(t *testing.T) (*systemhttp.Handler, *stubMailSettings, *stubReauth) {
	t.Helper()
	svc := &stubMailSettings{}
	reauth := &stubReauth{}
	handler := systemhttp.NewHandler(nil, nil, nil).
		WithMailSettings(svc).
		WithReauth(reauth)
	return handler, svc, reauth
}

// GET：master/admin 200，user 403；响应不回显口令，from/user 只给掩码。
func TestMailSettingsGetRolesAndMasking(t *testing.T) {
	handler, _, _ := newMailSettingsHandler(t)
	for _, role := range []coreauth.Role{coreauth.RoleMaster, coreauth.RoleAdmin} {
		c, rec := newContextWithEmail(http.MethodGet, "/api/admin/mail-settings", "", role, "m@example.com")
		handler.AdminMailSettings(c)
		if rec.Code != http.StatusOK {
			t.Fatalf("%s GET = %d: %s", role, rec.Code, rec.Body.String())
		}
		var body map[string]any
		if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
			t.Fatalf("decode body: %v", err)
		}
		pass, ok := body["pass"].(map[string]any)
		if !ok {
			t.Fatalf("pass = %#v", body["pass"])
		}
		if _, hasValue := pass["value"]; hasValue {
			t.Fatal("pass response carries a value")
		}
		if _, hasMasked := pass["masked"]; hasMasked {
			t.Fatal("pass response carries a mask")
		}
		for _, key := range []string{"from", "user"} {
			item, ok := body[key].(map[string]any)
			if !ok || item["masked"] != "****" {
				t.Fatalf("%s = %#v, want masked ****", key, body[key])
			}
			if _, hasValue := item["value"]; hasValue {
				t.Fatalf("%s response carries a value", key)
			}
		}
		if body["host"].(map[string]any)["value"] != "smtp.env.example" {
			t.Fatalf("host = %#v", body["host"])
		}
	}
	c, rec := newContextWithEmail(http.MethodGet, "/api/admin/mail-settings", "", coreauth.RoleUser, "u@example.com")
	handler.AdminMailSettings(c)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("user GET = %d, want 403", rec.Code)
	}
}

// 未注入用例时两端点 501：写面缺失必须显性。
func TestMailSettingsUnwiredIs501(t *testing.T) {
	handler := systemhttp.NewHandler(nil, nil, nil)
	c, rec := newContextWithEmail(http.MethodGet, "/api/admin/mail-settings", "", coreauth.RoleMaster, "m@example.com")
	handler.AdminMailSettings(c)
	if rec.Code != http.StatusNotImplemented {
		t.Fatalf("GET unwired = %d, want 501", rec.Code)
	}
	c, rec = newContextWithEmail(http.MethodPut, "/api/admin/mail-settings",
		`{"host":"smtp.new.example","current_password":"pw"}`, coreauth.RoleMaster, "m@example.com")
	handler.AdminMailSettingsSet(c, oapi.AdminMailSettingsSetParams{})
	if rec.Code != http.StatusNotImplemented {
		t.Fatalf("PUT unwired = %d, want 501", rec.Code)
	}
}

// PUT 只有 master：admin 与 user 都 403 零写入，二次口令不被调用。
func TestMailSettingsPutIsMasterOnly(t *testing.T) {
	handler, svc, reauth := newMailSettingsHandler(t)
	for _, role := range []coreauth.Role{coreauth.RoleAdmin, coreauth.RoleUser} {
		c, rec := newContextWithEmail(http.MethodPut, "/api/admin/mail-settings",
			`{"host":"smtp.new.example","current_password":"pw"}`, role, "x@example.com")
		handler.AdminMailSettingsSet(c, oapi.AdminMailSettingsSetParams{})
		if rec.Code != http.StatusForbidden {
			t.Fatalf("%s PUT = %d, want 403", role, rec.Code)
		}
	}
	if svc.sets != 0 || reauth.calls != 0 {
		t.Fatalf("被拒写有副作用: sets=%d reauth=%d", svc.sets, reauth.calls)
	}
}

// current_password 缺失/空/null/非字符串都 400 零写入；口令错 403 零写入。
func TestMailSettingsPutRequiresCurrentPassword(t *testing.T) {
	for _, body := range []string{
		`{"host":"smtp.new.example"}`,
		`{"host":"smtp.new.example","current_password":""}`,
		`{"host":"smtp.new.example","current_password":null}`,
		`{"host":"smtp.new.example","current_password":123}`,
	} {
		handler, svc, reauth := newMailSettingsHandler(t)
		c, rec := newContextWithEmail(http.MethodPut, "/api/admin/mail-settings", body,
			coreauth.RoleMaster, "m@example.com")
		handler.AdminMailSettingsSet(c, oapi.AdminMailSettingsSetParams{})
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("body %s = %d, want 400", body, rec.Code)
		}
		if svc.sets != 0 || reauth.calls != 0 {
			t.Fatalf("缺失口令有副作用: sets=%d reauth=%d", svc.sets, reauth.calls)
		}
	}

	handler, svc, _ := newMailSettingsHandler(t)
	handler.WithReauth(&stubReauth{err: errs.New(errs.CodeForbidden, "wrong password")})
	c, rec := newContextWithEmail(http.MethodPut, "/api/admin/mail-settings",
		`{"host":"smtp.new.example","current_password":"wrong"}`, coreauth.RoleMaster, "m@example.com")
	handler.AdminMailSettingsSet(c, oapi.AdminMailSettingsSetParams{})
	if rec.Code != http.StatusForbidden {
		t.Fatalf("wrong password = %d, want 403", rec.Code)
	}
	if svc.sets != 0 {
		t.Fatalf("口令错仍写库: sets=%d", svc.sets)
	}
}

// PUT 成功：二次口令被调用，字段折成写意图；200 回报告。
func TestMailSettingsPutSuccess(t *testing.T) {
	handler, svc, reauth := newMailSettingsHandler(t)
	c, rec := newContextWithEmail(http.MethodPut, "/api/admin/mail-settings",
		`{"host":"smtp.new.example","port":587,"tls_mode":"starttls","from":"a@example.com","user":"u","pass":"p","current_password":"pw"}`,
		coreauth.RoleMaster, "m@example.com")
	handler.AdminMailSettingsSet(c, oapi.AdminMailSettingsSetParams{})
	if rec.Code != http.StatusOK {
		t.Fatalf("PUT = %d: %s", rec.Code, rec.Body.String())
	}
	if reauth.calls != 1 || reauth.password != "pw" {
		t.Fatalf("reauth = %d/%q", reauth.calls, reauth.password)
	}
	if svc.sets != 1 || len(svc.changes) != 6 {
		t.Fatalf("sets=%d changes=%+v", svc.sets, svc.changes)
	}
	byKey := map[string]appsettings.MailChange{}
	for _, change := range svc.changes {
		byKey[change.Key] = change
	}
	if byKey[appsettings.KeyMailSMTPPort].Value != 587 {
		t.Fatalf("port change = %+v", byKey[appsettings.KeyMailSMTPPort])
	}
	if byKey[appsettings.KeyMailTLSMode].Value != "starttls" {
		t.Fatalf("tls change = %+v", byKey[appsettings.KeyMailTLSMode])
	}
	if byKey[appsettings.KeyMailPass].Value != "p" {
		t.Fatalf("pass change = %+v", byKey[appsettings.KeyMailPass])
	}
}

// 部分补丁：只带 host 时只产生一条写意图；null 是 clear。
func TestMailSettingsPutPartialAndClear(t *testing.T) {
	handler, svc, _ := newMailSettingsHandler(t)
	c, rec := newContextWithEmail(http.MethodPut, "/api/admin/mail-settings",
		`{"host":"smtp.new.example","current_password":"pw"}`, coreauth.RoleMaster, "m@example.com")
	handler.AdminMailSettingsSet(c, oapi.AdminMailSettingsSetParams{})
	if rec.Code != http.StatusOK {
		t.Fatalf("partial PUT = %d: %s", rec.Code, rec.Body.String())
	}
	if len(svc.changes) != 1 || svc.changes[0].Key != appsettings.KeyMailSMTPHost ||
		svc.changes[0].Op != appsettings.OpSet {
		t.Fatalf("partial changes = %+v", svc.changes)
	}

	c, rec = newContextWithEmail(http.MethodPut, "/api/admin/mail-settings",
		`{"host":null,"current_password":"pw"}`, coreauth.RoleMaster, "m@example.com")
	handler.AdminMailSettingsSet(c, oapi.AdminMailSettingsSetParams{})
	if rec.Code != http.StatusOK {
		t.Fatalf("clear PUT = %d: %s", rec.Code, rec.Body.String())
	}
	if len(svc.changes) != 1 || svc.changes[0].Op != appsettings.OpClear {
		t.Fatalf("clear changes = %+v", svc.changes)
	}
}

// 负例：未知字段、类型错、空更新（只有口令）都 400 零写入，二次口令不被调用。
func TestMailSettingsPutRejectsInvalidBodies(t *testing.T) {
	for _, body := range []string{
		`{"unknown":"x","current_password":"pw"}`,
		`{"port":"587","current_password":"pw"}`,
		`{"port":1.5,"current_password":"pw"}`,
		`{"host":true,"current_password":"pw"}`,
		`{"current_password":"pw"}`,
	} {
		handler, svc, reauth := newMailSettingsHandler(t)
		c, rec := newContextWithEmail(http.MethodPut, "/api/admin/mail-settings", body,
			coreauth.RoleMaster, "m@example.com")
		handler.AdminMailSettingsSet(c, oapi.AdminMailSettingsSetParams{})
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("body %s = %d, want 400", body, rec.Code)
		}
		if svc.sets != 0 || reauth.calls != 0 {
			t.Fatalf("被拒写有副作用: sets=%d reauth=%d", svc.sets, reauth.calls)
		}
	}
}

// 二次口令端口未接线：写面 500 失败关闭，不静默落库。
func TestMailSettingsPutWithoutReauthIsInternal(t *testing.T) {
	handler := systemhttp.NewHandler(nil, nil, nil).WithMailSettings(&stubMailSettings{})
	c, rec := newContextWithEmail(http.MethodPut, "/api/admin/mail-settings",
		`{"host":"smtp.new.example","current_password":"pw"}`, coreauth.RoleMaster, "m@example.com")
	handler.AdminMailSettingsSet(c, oapi.AdminMailSettingsSetParams{})
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("unwired reauth = %d, want 500; body=%s", rec.Code, rec.Body.String())
	}
}

// 响应体不得出现口令明文或密文占位；只允许 set/source。
func TestMailSettingsResponseHasNoSecret(t *testing.T) {
	handler, _, _ := newMailSettingsHandler(t)
	c, rec := newContextWithEmail(http.MethodGet, "/api/admin/mail-settings", "", coreauth.RoleMaster, "m@example.com")
	handler.AdminMailSettings(c)
	if rec.Code != http.StatusOK {
		t.Fatalf("GET = %d", rec.Code)
	}
	if strings.Contains(rec.Body.String(), "env-pass") {
		t.Fatalf("响应泄漏口令: %s", rec.Body.String())
	}
}
