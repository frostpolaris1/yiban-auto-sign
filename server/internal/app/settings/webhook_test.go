package settings_test

// 测试推送用例判据：出口未配置 409、失败 502（错误文本只含固定文案）、
// 冷却 429、每次外呼一条 settings.webhook_test 审计、
// 凭据不进错误文本与审计。

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	appsettings "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/settings"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corewebhook "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/webhook"
)

// fakeHook 记录最后一次推送，并按需返回错误。
type fakeHook struct {
	push  corewebhook.Push
	err   error
	sends int
}

func (f *fakeHook) Send(_ context.Context, push corewebhook.Push) error {
	f.sends++
	f.push = push
	return f.err
}

func newWebhookService(t *testing.T, hook corewebhook.Sender, cooldown appsettings.Cooldown) (*appsettings.Service, *fakeAudit, *fakeTx) {
	t.Helper()
	audits := &fakeAudit{}
	tx := &fakeTx{}
	svc, err := appsettings.NewService(appsettings.Deps{
		Store: &fakeStore{}, Audit: audits, Tx: tx, Clock: fixedClock{},
		Webhook: hook, WebhookCooldown: cooldown,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc, audits, tx
}

// 出口未配置：409，不静默成功、不写审计。
func TestSendTestWebhookDisabled(t *testing.T) {
	svc, audits, _ := newWebhookService(t, nil, nil)
	err := svc.SendTestWebhook(context.Background(), coreauth.User{ID: "master-1"})
	if code := errs.From(err).Code; code != errs.CodeConflict {
		t.Fatalf("code = %s, want conflict", code)
	}
	if len(audits.entries) != 0 {
		t.Fatal("a disabled outlet wrote an audit row")
	}
}

// 成功：恰一条 settings.webhook_test 审计（result=sent），冷却键独立前缀。
func TestSendTestWebhookSuccess(t *testing.T) {
	hook := &fakeHook{}
	cooldown := &fakeCooldown{allowed: true}
	svc, audits, tx := newWebhookService(t, hook, cooldown)
	if err := svc.SendTestWebhook(context.Background(), coreauth.User{ID: "master-1"}); err != nil {
		t.Fatalf("SendTestWebhook: %v", err)
	}
	if hook.sends != 1 {
		t.Fatalf("sends = %d, want 1", hook.sends)
	}
	if hook.push.Title == "" || hook.push.Content == "" {
		t.Fatalf("push = %+v, want title and content", hook.push)
	}
	if len(cooldown.keys) != 1 || cooldown.keys[0] != "admin-webhook-test:master-1" {
		t.Fatalf("cooldown keys = %v, want independent prefix", cooldown.keys)
	}
	if tx.calls != 1 {
		t.Fatalf("audit transactions = %d, want 1", tx.calls)
	}
	if len(audits.entries) != 1 {
		t.Fatalf("audit entries = %d, want 1", len(audits.entries))
	}
	entry := audits.entries[0]
	if entry.Action != coreaudit.ActionSettingsWebhookTest ||
		entry.TargetType != "settings" || entry.TargetID != "webhook" {
		t.Fatalf("audit entry = %+v", entry)
	}
	if entry.Detail["result"] != "sent" {
		t.Fatalf("audit detail = %v, want result=sent", entry.Detail)
	}
}

// 投递失败：502，错误文本与审计 detail 都不含对端细节或凭据。
func TestSendTestWebhookFailureHidesOutletDetail(t *testing.T) {
	const secret = "SCT_FAKE_TEST_KEY_000000"
	hook := &fakeHook{err: &corewebhook.DeliveryError{
		Kind: corewebhook.KindRefused,
		Err:  errors.New("POST https://sctapi.ftqq.com/" + secret + ".send: refused"),
	}}
	svc, audits, _ := newWebhookService(t, hook, nil)
	err := svc.SendTestWebhook(context.Background(), coreauth.User{ID: "master-1"})
	if code := errs.From(err).Code; code != errs.CodeUpstream {
		t.Fatalf("code = %s, want upstream_unavailable", code)
	}
	if strings.Contains(err.Error(), secret) || strings.Contains(err.Error(), "sctapi") {
		t.Fatalf("error text leaks outlet detail: %v", err)
	}
	if len(audits.entries) != 1 {
		t.Fatalf("audit entries = %d, want 1", len(audits.entries))
	}
	detail := audits.entries[0].Detail
	if detail["result"] != "error" || detail["kind"] != "refused" || detail["retryable"] != false {
		t.Fatalf("audit detail = %v, want error/refused/false", detail)
	}
	if s, _ := detail["kind"].(string); strings.Contains(s, secret) {
		t.Fatalf("audit kind leaks the secret: %q", s)
	}
}

// 冷却内重复请求：429 且带 retry_after_seconds，不触达出口。
func TestSendTestWebhookCooldown(t *testing.T) {
	hook := &fakeHook{}
	cooldown := &fakeCooldown{allowed: false, wait: 42 * time.Second}
	svc, audits, _ := newWebhookService(t, hook, cooldown)
	err := svc.SendTestWebhook(context.Background(), coreauth.User{ID: "master-1"})
	e := errs.From(err)
	if e.Code != errs.CodeRateLimited {
		t.Fatalf("code = %s, want rate_limited", e.Code)
	}
	if seconds, ok := e.Details["retry_after_seconds"].(int); !ok || seconds != 42 {
		t.Fatalf("retry_after_seconds = %v, want 42", e.Details["retry_after_seconds"])
	}
	if hook.sends != 0 {
		t.Fatal("a throttled request reached the outlet")
	}
	if len(audits.entries) != 0 {
		t.Fatal("a throttled request wrote an audit row")
	}
}

// 审计写失败：回 internal，审计完整性优先于本轮响应。
func TestSendTestWebhookAuditFailure(t *testing.T) {
	hook := &fakeHook{}
	audits := &fakeAudit{err: errors.New("audit write failed")}
	svc, err := appsettings.NewService(appsettings.Deps{
		Store: &fakeStore{}, Audit: audits, Tx: &fakeTx{}, Clock: fixedClock{},
		Webhook: hook,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	err = svc.SendTestWebhook(context.Background(), coreauth.User{ID: "master-1"})
	if code := errs.From(err).Code; code != errs.CodeInternal {
		t.Fatalf("code = %s, want internal", code)
	}
}
