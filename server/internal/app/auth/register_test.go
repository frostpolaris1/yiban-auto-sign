package auth_test

// 公开注册与注册开关的进程内用例测试。
//
// # 功能
// 钉住四条判据：关闭时不付 KDF；校验失败不建用户；成功建号并签发会话；
// 开关只有 master 能写。
// 本文件**不**连数据库，**不**发 HTTP。
//
// # 口径
// 替身只实现端口方法，不模拟 SQL 形状。真库行为由 cmd/server 的 e2e 负责。

import (
	"context"
	"testing"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/auth"
	appuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/user"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

var registerActor = appaudit.Actor{
	UserID: "master-1",
	Role:   coreauth.RoleMaster,
	Label:  "m***@example.com",
}

func validRegisterInput() appauth.RegisterInput {
	return appauth.RegisterInput{
		Email:          "new-user@example.com",
		Password:       "Correct-horse-9!",
		AgreeTerms:     true,
		AgreePrivacy:   true,
		TermsVersion:   appauth.TermsVersion,
		PrivacyVersion: appauth.PrivacyVersion,
	}
}

// 关闭注册时在 KDF 之前拒绝，且不建用户。
func TestRegisterClosedDoesNotHash(t *testing.T) {
	h := newHarness(t)
	h.registrations.enabled = false

	if _, err := h.svc.Register(context.Background(), validRegisterInput()); errs.From(err).Code != errs.CodeForbidden {
		t.Fatalf("Register while closed = %v, want forbidden", err)
	}
	if h.kdf.hashes != 0 {
		t.Fatalf("closed registration hashed %d times; want 0", h.kdf.hashes)
	}
	if len(h.users.byEmail) != 0 {
		t.Fatalf("closed registration created %d users", len(h.users.byEmail))
	}
}

// 成功注册建 active/user 账号、签发会话并写 auth.register。
func TestRegisterCreatesUserAndSession(t *testing.T) {
	h := newHarness(t)
	h.registrations.enabled = true

	issued, err := h.svc.Register(context.Background(), validRegisterInput())
	if err != nil {
		t.Fatalf("Register: %v", err)
	}
	if issued.Token == "" || issued.Session.CSRFToken == "" {
		t.Fatalf("issued session is incomplete: %+v", issued)
	}
	if issued.Session.User.Email != "new-user@example.com" {
		t.Fatalf("session email = %q", issued.Session.User.Email)
	}
	record, ok := h.users.byEmail["new-user@example.com"]
	if !ok {
		t.Fatal("Register did not create the user")
	}
	if record.Role != coreauth.RoleUser || record.Status != coreauth.StatusActive || record.MustChangePassword {
		t.Fatalf("stored user = %+v, want user/active/no forced change", record)
	}
	if len(h.sessions.issued) != 1 {
		t.Fatalf("sessions issued = %d, want 1", len(h.sessions.issued))
	}
	found := false
	for _, action := range h.audits.actions {
		if action == coreaudit.ActionAuthRegister {
			found = true
		}
	}
	if !found {
		t.Fatalf("audit actions = %v, want auth.register", h.audits.actions)
	}
}

// 校验失败一律不建用户，也不付 KDF。
func TestRegisterRejectsInvalidInput(t *testing.T) {
	cases := []struct {
		name   string
		mutate func(*appauth.RegisterInput)
		code   errs.Code
	}{
		{"weak password", func(in *appauth.RegisterInput) { in.Password = "short" }, errs.CodeValidation},
		{"no terms", func(in *appauth.RegisterInput) { in.AgreeTerms = false }, errs.CodeValidation},
		{"no privacy", func(in *appauth.RegisterInput) { in.AgreePrivacy = false }, errs.CodeValidation},
		{"missing terms version", func(in *appauth.RegisterInput) { in.TermsVersion = 0 }, errs.CodeValidation},
		{"missing privacy version", func(in *appauth.RegisterInput) { in.PrivacyVersion = 0 }, errs.CodeValidation},
		{"stale terms", func(in *appauth.RegisterInput) { in.TermsVersion++ }, errs.CodeConflict},
		{"stale privacy", func(in *appauth.RegisterInput) { in.PrivacyVersion++ }, errs.CodeConflict},
		{"invalid email", func(in *appauth.RegisterInput) { in.Email = "not-an-email" }, errs.CodeValidation},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			h := newHarness(t)
			h.registrations.enabled = true
			in := validRegisterInput()
			tc.mutate(&in)
			if _, err := h.svc.Register(context.Background(), in); errs.From(err).Code != tc.code {
				t.Fatalf("Register = %v, want %s", err, tc.code)
			}
			if h.kdf.hashes != 0 {
				t.Fatalf("rejected registration hashed %d times; want 0", h.kdf.hashes)
			}
			if len(h.users.byEmail) != 0 {
				t.Fatalf("rejected registration created %d users", len(h.users.byEmail))
			}
		})
	}
}

// 重复邮箱：KDF 已经付过，返回 conflict 与不区分大小写的文案。
func TestRegisterMapsDuplicateEmail(t *testing.T) {
	h := newHarness(t)
	h.registrations.enabled = true
	h.users.byEmail["new-user@example.com"] = appuser.Record{
		ID:     "existing",
		Email:  "new-user@example.com",
		Role:   coreauth.RoleUser,
		Status: coreauth.StatusActive,
	}
	_, err := h.svc.Register(context.Background(), validRegisterInput())
	if errs.From(err).Code != errs.CodeConflict {
		t.Fatalf("duplicate registration = %v, want conflict", err)
	}
	if h.kdf.hashes != 1 {
		t.Fatalf("duplicate registration hashed %d times; want 1 (timing parity)", h.kdf.hashes)
	}
}

// 容量错误原样上抛，不伪装成邮箱冲突。
func TestRegisterPropagatesCapacity(t *testing.T) {
	h := newHarness(t)
	h.registrations.enabled = true
	h.users.createErr = errs.New(errs.CodeCapacity, "user capacity exceeded")
	if _, err := h.svc.Register(context.Background(), validRegisterInput()); errs.From(err).Code != errs.CodeCapacity {
		t.Fatalf("capacity registration = %v, want capacity_exceeded", err)
	}
}

// 只有 master 能写开关；写入落库并留审计。
func TestSetRegistrationMasterOnly(t *testing.T) {
	h := newHarness(t)
	if _, err := h.svc.SetRegistration(context.Background(), corescope.User("u-1"), registerActor, true); errs.From(err).Code != errs.CodeForbidden {
		t.Fatalf("user switch = %v, want forbidden", err)
	}
	if len(h.registrations.set) != 0 {
		t.Fatalf("a forbidden switch wrote %d times", len(h.registrations.set))
	}

	status, err := h.svc.SetRegistration(context.Background(), corescope.Master(), registerActor, true)
	if err != nil {
		t.Fatalf("master switch: %v", err)
	}
	if !status.Enabled || status.TermsVersion != appauth.TermsVersion || status.PrivacyVersion != appauth.PrivacyVersion {
		t.Fatalf("switch status = %+v", status)
	}
	if len(h.registrations.set) != 1 || !h.registrations.set[0] {
		t.Fatalf("store writes = %v, want [true]", h.registrations.set)
	}
	found := false
	for _, action := range h.audits.actions {
		if action == coreaudit.ActionSettingsRegistration {
			found = true
		}
	}
	if !found {
		t.Fatalf("audit actions = %v, want settings.registration", h.audits.actions)
	}
}

// 公开状态始终带协议版本；关闭时也一样。
func TestRegistrationStatusCarriesVersions(t *testing.T) {
	h := newHarness(t)
	status, err := h.svc.Registration(context.Background())
	if err != nil {
		t.Fatalf("Registration: %v", err)
	}
	if status.Enabled {
		t.Fatal("a fresh harness reports registration enabled")
	}
	if status.TermsVersion != appauth.TermsVersion || status.PrivacyVersion != appauth.PrivacyVersion {
		t.Fatalf("versions = %d/%d", status.TermsVersion, status.PrivacyVersion)
	}
}
