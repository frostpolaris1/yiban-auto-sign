package system_test

// AdminWebhookTest 端点判据：user/admin 403、未配置 409、成功 200、
// 失败 502 且响应不含凭据、冷却 429+Retry-After。

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"testing"
	"time"

	appsettings "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/settings"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	corewebhook "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/webhook"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	systemhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/system"
)

// fakeOutlet 记录最后一次推送，并按需返回错误。
type fakeOutlet struct {
	push  corewebhook.Push
	err   error
	sends int
}

func (f *fakeOutlet) Send(_ context.Context, push corewebhook.Push) error {
	f.sends++
	f.push = push
	return f.err
}

// newWebhookHandler 构造带 webhook 端口的处理器。
func newWebhookHandler(t *testing.T, hook corewebhook.Sender, cooldown appsettings.Cooldown) *systemhttp.Handler {
	t.Helper()
	roster, err := appsettings.NewRoster([]appsettings.Entry{
		{Key: "YIBAN_WORKERS", Group: "调度", Default: float64(1), HasDefault: true},
	}, mapSource{})
	if err != nil {
		t.Fatalf("roster: %v", err)
	}
	announcementSvc, err := appsettings.NewService(appsettings.Deps{
		Store: &memAnnouncementStore{}, Audit: &memAudit{}, Tx: passthroughTx{},
		Clock: fixedClock{}, Webhook: hook, WebhookCooldown: cooldown,
	})
	if err != nil {
		t.Fatalf("announcement service: %v", err)
	}
	return systemhttp.NewHandler(roster, announcementSvc, announcementSvc).
		WithWebhookTest(announcementSvc)
}

// 角色门：未登录 401；admin/user 403。
func TestAdminWebhookTestMasterOnly(t *testing.T) {
	handler, _, _ := newHandlerWith(t, &fakeSender{}, nil)

	c, rec := newContext(http.MethodPost, "/api/admin/webhook/test", "", "")
	handler.AdminWebhookTest(c, oapi.AdminWebhookTestParams{})
	if rec.Code != http.StatusUnauthorized || errorCode(t, rec) != "unauthorized" {
		t.Fatalf("anonymous = %d %s", rec.Code, rec.Body.String())
	}
	for _, role := range []coreauth.Role{coreauth.RoleUser, coreauth.RoleAdmin} {
		c, rec = newContext(http.MethodPost, "/api/admin/webhook/test", "", role)
		handler.AdminWebhookTest(c, oapi.AdminWebhookTestParams{})
		if rec.Code != http.StatusForbidden || errorCode(t, rec) != "forbidden" {
			t.Fatalf("%s = %d %s", role, rec.Code, rec.Body.String())
		}
	}
}

// 端点未注入用例时 501：生产装配必须注入，缺失必须显性。
func TestAdminWebhookTestWithoutUseCase(t *testing.T) {
	handler, _, _ := newHandlerWith(t, &fakeSender{}, nil)
	c, rec := newContext(http.MethodPost, "/api/admin/webhook/test", "", coreauth.RoleMaster)
	handler.AdminWebhookTest(c, oapi.AdminWebhookTestParams{})
	if rec.Code != http.StatusNotImplemented {
		t.Fatalf("status = %d, want 501", rec.Code)
	}
}

// 出口未配置 409；成功 200 且固定文案；失败 502 且响应不含凭据；
// 冷却 429 + Retry-After 且不触达出口。
func TestAdminWebhookTestOutcomes(t *testing.T) {
	t.Run("disabled 409", func(t *testing.T) {
		handler := newWebhookHandler(t, nil, nil)
		c, rec := newContext(http.MethodPost, "/api/admin/webhook/test", "", coreauth.RoleMaster)
		handler.AdminWebhookTest(c, oapi.AdminWebhookTestParams{})
		if rec.Code != http.StatusConflict || errorCode(t, rec) != "conflict" {
			t.Fatalf("status = %d %s", rec.Code, rec.Body.String())
		}
	})
	t.Run("success 200", func(t *testing.T) {
		handler := newWebhookHandler(t, &fakeOutlet{}, nil)
		c, rec := newContext(http.MethodPost, "/api/admin/webhook/test", "", coreauth.RoleMaster)
		handler.AdminWebhookTest(c, oapi.AdminWebhookTestParams{})
		if rec.Code != http.StatusOK {
			t.Fatalf("status = %d %s", rec.Code, rec.Body.String())
		}
		var body struct {
			Message string `json:"message"`
		}
		if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
			t.Fatalf("decode: %v", err)
		}
		if body.Message == "" {
			t.Fatal("success answer carries no message")
		}
	})
	t.Run("failure 502 without secret", func(t *testing.T) {
		const secret = "SCT_FAKE_TEST_KEY_000000"
		hook := &fakeOutlet{err: &corewebhook.DeliveryError{
			Kind: corewebhook.KindRefused,
			Err:  errors.New("https://sctapi.ftqq.com/" + secret + ".send refused"),
		}}
		handler := newWebhookHandler(t, hook, nil)
		c, rec := newContext(http.MethodPost, "/api/admin/webhook/test", "", coreauth.RoleMaster)
		handler.AdminWebhookTest(c, oapi.AdminWebhookTestParams{})
		if rec.Code != http.StatusBadGateway || errorCode(t, rec) != "upstream_unavailable" {
			t.Fatalf("status = %d %s", rec.Code, rec.Body.String())
		}
		if strings.Contains(rec.Body.String(), secret) {
			t.Fatalf("response leaks the secret: %s", rec.Body.String())
		}
	})
	t.Run("cooldown 429 with retry-after", func(t *testing.T) {
		hook := &fakeOutlet{}
		cooldown := &webhookCooldown{allowed: false, wait: 42 * time.Second}
		handler := newWebhookHandler(t, hook, cooldown)
		c, rec := newContext(http.MethodPost, "/api/admin/webhook/test", "", coreauth.RoleMaster)
		handler.AdminWebhookTest(c, oapi.AdminWebhookTestParams{})
		if rec.Code != http.StatusTooManyRequests || errorCode(t, rec) != "rate_limited" {
			t.Fatalf("status = %d %s", rec.Code, rec.Body.String())
		}
		if got := rec.Header().Get("Retry-After"); got != "42" {
			t.Fatalf("Retry-After = %q, want 42", got)
		}
		if hook.sends != 0 {
			t.Fatal("a throttled request reached the outlet")
		}
	})
}

// webhookCooldown 是固定判据的冷却替身（与 mail 测试的 fakeCooldown 同形，
// 独立命名避免类型冲突）。
type webhookCooldown struct {
	allowed bool
	wait    time.Duration
}

func (f *webhookCooldown) Allow(string) (bool, time.Duration) {
	return f.allowed, f.wait
}
