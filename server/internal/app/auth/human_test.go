package auth_test

// 公开注册的人机验证用例测试。
//
// # 功能
// 钉住三条判据：on 档下令牌缺失/无效/服务不可达都在 KDF 之前拒绝且不建号；
// log 档下任何失败都放行；站点密钥只在配置了验证端口时随注册状态回给前端。
// 本文件**不**连数据库，**不**发 HTTP。
//
// # 口径
// 端点行为由 fakeHuman 钉住；真实 siteverify 由 adapters/turnstile 的测试负责。

import (
	"context"
	"testing"

	appauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

// fakeHuman 记录调用参数，按固定结果回复。
type fakeHuman struct {
	ok     bool
	codes  []string
	err    error
	calls  int
	token  string
	remote string
}

func (f *fakeHuman) Verify(_ context.Context, token, remoteIP string) (bool, []string, error) {
	f.calls++
	f.token, f.remote = token, remoteIP
	return f.ok, f.codes, f.err
}

// newHumanHarness 在标准替身上注入人机验证端口。
func newHumanHarness(t *testing.T, human appauth.HumanVerifier, siteKey string, enforce bool) *harness {
	t.Helper()
	return newHarnessWith(t, func(d *appauth.Deps) {
		d.Human = human
		d.HumanSiteKey = siteKey
		d.HumanEnforce = enforce
	})
}

// on 档缺令牌：在 KDF 之前回 400 human_verification_failed，不调端点、不建号。
func TestRegisterHumanEnforceRejectsMissingToken(t *testing.T) {
	human := &fakeHuman{ok: true}
	h := newHumanHarness(t, human, "site-key", true)
	h.registrations.enabled = true

	if _, err := h.svc.Register(context.Background(), validRegisterInput()); errs.From(err).Code != errs.CodeHumanVerification {
		t.Fatalf("register without token = %v, want human_verification_failed", err)
	}
	if human.calls != 0 {
		t.Fatalf("verifier calls = %d, want 0 (empty token is not sent)", human.calls)
	}
	if h.kdf.hashes.Load() != 0 {
		t.Fatalf("KDF hash calls = %d, want 0", h.kdf.hashes.Load())
	}
	if len(h.users.byEmail) != 0 {
		t.Fatalf("rejected registration created %d users", len(h.users.byEmail))
	}
}

// on 档端点拒绝：400 human_verification_failed，不付 KDF。
func TestRegisterHumanEnforceRejectsToken(t *testing.T) {
	human := &fakeHuman{ok: false, codes: []string{"invalid-input-response"}}
	h := newHumanHarness(t, human, "site-key", true)
	h.registrations.enabled = true
	in := validRegisterInput()
	in.TurnstileToken = "bad-token"

	if _, err := h.svc.Register(context.Background(), in); errs.From(err).Code != errs.CodeHumanVerification {
		t.Fatalf("register with rejected token = %v, want human_verification_failed", err)
	}
	if human.calls != 1 {
		t.Fatalf("verifier calls = %d, want 1", human.calls)
	}
	if h.kdf.hashes.Load() != 0 {
		t.Fatalf("KDF hash calls = %d, want 0", h.kdf.hashes.Load())
	}
}

// on 档端点不可达：502 upstream，与「用户没通过」严格区分。
func TestRegisterHumanEnforceMapsVerifierFailureToUpstream(t *testing.T) {
	human := &fakeHuman{err: errs.New(errs.CodeUpstream, "boom")}
	h := newHumanHarness(t, human, "site-key", true)
	h.registrations.enabled = true
	in := validRegisterInput()
	in.TurnstileToken = "token"

	if _, err := h.svc.Register(context.Background(), in); errs.From(err).Code != errs.CodeUpstream {
		t.Fatalf("register with unavailable verifier = %v, want upstream", err)
	}
	if h.kdf.hashes.Load() != 0 {
		t.Fatalf("KDF hash calls = %d, want 0", h.kdf.hashes.Load())
	}
}

// on 档通过：令牌与客户端地址原样交给端点，随后正常建号。
func TestRegisterHumanEnforceAcceptsToken(t *testing.T) {
	human := &fakeHuman{ok: true}
	h := newHumanHarness(t, human, "site-key", true)
	h.registrations.enabled = true
	in := validRegisterInput()
	in.TurnstileToken = "  good-token  "
	in.RemoteIP = "203.0.113.7"

	if _, err := h.svc.Register(context.Background(), in); err != nil {
		t.Fatalf("Register: %v", err)
	}
	if human.calls != 1 || human.token != "good-token" || human.remote != "203.0.113.7" {
		t.Fatalf("verifier saw token=%q ip=%q calls=%d", human.token, human.remote, human.calls)
	}
	if _, ok := h.users.byEmail["new-user@example.com"]; !ok {
		t.Fatal("accepted registration did not create the user")
	}
}

// log 档拒绝令牌：只记日志，注册照常完成。
func TestRegisterHumanLogModeLetsRejectedTokenThrough(t *testing.T) {
	human := &fakeHuman{ok: false, codes: []string{"invalid-input-response"}}
	h := newHumanHarness(t, human, "site-key", false)
	h.registrations.enabled = true
	in := validRegisterInput()
	in.TurnstileToken = "bad-token"

	if _, err := h.svc.Register(context.Background(), in); err != nil {
		t.Fatalf("log mode Register: %v", err)
	}
	if human.calls != 1 {
		t.Fatalf("verifier calls = %d, want 1", human.calls)
	}
	if _, ok := h.users.byEmail["new-user@example.com"]; !ok {
		t.Fatal("log mode blocked the registration")
	}
}

// 注册状态只在配了站点密钥时把密钥回给前端。
func TestRegistrationStatusCarriesSiteKeyOnlyWhenConfigured(t *testing.T) {
	on := newHumanHarness(t, &fakeHuman{ok: true}, "site-key", true)
	on.registrations.enabled = true
	status, err := on.svc.Registration(context.Background())
	if err != nil {
		t.Fatalf("Registration: %v", err)
	}
	if status.TurnstileSiteKey != "site-key" {
		t.Fatalf("site key = %q, want site-key", status.TurnstileSiteKey)
	}
	if !status.TurnstileEnforced {
		t.Fatal("on mode must report turnstile_enforced")
	}

	// log 档：控件照样渲染（站点密钥非空），但服务端不拦。
	logMode := newHumanHarness(t, &fakeHuman{ok: false}, "site-key", false)
	logMode.registrations.enabled = true
	status, err = logMode.svc.Registration(context.Background())
	if err != nil {
		t.Fatalf("Registration (log mode): %v", err)
	}
	if status.TurnstileSiteKey != "site-key" || status.TurnstileEnforced {
		t.Fatalf("log mode status = %+v, want site key without enforcement", status)
	}

	off := newHarness(t)
	off.registrations.enabled = true
	status, err = off.svc.Registration(context.Background())
	if err != nil {
		t.Fatalf("Registration: %v", err)
	}
	if status.TurnstileSiteKey != "" || status.TurnstileEnforced {
		t.Fatalf("status without verifier = %+v, want empty key and no enforcement", status)
	}
}
