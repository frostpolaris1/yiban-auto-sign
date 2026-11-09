package main

// 2d webhook 渠道的真库 e2e：Dispatcher + 真 PostgreSQL + 假 webhook 端口。
//
// 覆盖：仅 webhook 订阅启用时锚行 + webhook 账本行；
// 端口 nil → skipped(webhook_not_configured)；
// 两个订阅者同事件恰一条真发、其余 skipped(cooldown_hit)；
// 未配置端口与已配置端口的两形态都落真实账本行。

import (
	"context"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	appnotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/notify"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
	corewebhook "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/webhook"
)

// scriptedHook 是脚本化的 webhook 端口替身。
type scriptedHook struct {
	mu     sync.Mutex
	sends  int
	pushes []corewebhook.Push
}

func (h *scriptedHook) Send(_ context.Context, push corewebhook.Push) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.sends++
	h.pushes = append(h.pushes, push)
	return nil
}

// webhookDispatcher 用真库仓储构造带 webhook 端口的投递服务。
func (e *notifyDeliveryEnv) webhookDispatcher(hook corewebhook.Sender, clk *notifyTestClock) *appnotify.Dispatcher {
	e.t.Helper()
	dispatcher, err := appnotify.NewDispatcher(appnotify.DispatcherDeps{
		Notes:    pg.NewNotificationRepo(e.db),
		Ledger:   pg.NewNotificationDeliveryRepo(e.db),
		Audience: pg.NewNotificationAudienceRepo(e.db),
		Webhook:  hook,
		// 节流 60s / 额度 5 与生产缺省同值；窗口用 UTC 简化断言。
		WebhookCooldown: time.Minute, WebhookDailyMax: 5,
		Location: time.UTC, Clock: clk,
		Logger: slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if err != nil {
		e.t.Fatalf("NewDispatcher: %v", err)
	}
	return dispatcher
}

// webhookOnlyPrefs 显式只开 webhook。
func webhookOnlyPrefs(category corenotify.Category) []corenotify.Pref {
	return []corenotify.Pref{
		{Category: category, Channel: corenotify.ChannelInApp, Enabled: false},
		{Category: category, Channel: corenotify.ChannelEmail, Enabled: false},
		{Category: category, Channel: corenotify.ChannelWebhook, Enabled: true},
	}
}

// 端口 nil：锚行与 webhook 账本行都落库，账本行立即 skipped(webhook_not_configured)。
func TestNotifyWebhookNotConfiguredEndToEnd(t *testing.T) {
	e := newNotifyDeliveryEnv(t)
	school := e.school("wh-off")
	master := e.admin("wh-off-master@example.com", coreauth.RoleMaster)
	if err := pg.NewNotificationPrefsRepo(e.db).Replace(e.ctx, master.ID,
		webhookOnlyPrefs(corenotify.CategoryCredentialInvalid)); err != nil {
		t.Fatalf("replace prefs: %v", err)
	}
	account := e.account(school, master.ID, "13800000001")
	clk := &notifyTestClock{now: time.Date(2026, 11, 10, 6, 0, 0, 0, time.UTC)}
	dispatcher := e.webhookDispatcher(nil, clk)

	event := corenotify.Event{
		Category:  corenotify.CategoryCredentialInvalid,
		SchoolID:  school,
		AccountID: account,
		DedupeKey: "credential_invalid/wh-off/1",
		Day:       "2026-11-10",
	}
	if err := dispatcher.Dispatch(e.ctx, event); err != nil {
		t.Fatalf("Dispatch: %v", err)
	}
	if got := e.scalarInt(`SELECT count(*) FROM notifications WHERE user_id = $1::uuid`, master.ID); got != 1 {
		t.Fatalf("锚行 = %d, want 1", got)
	}
	if got := e.scalarInt(`
		SELECT count(*) FROM notification_deliveries d
		  JOIN notifications n ON n.id = d.notification_id
		 WHERE n.user_id = $1::uuid AND d.channel = 'webhook'`, master.ID); got != 1 {
		t.Fatalf("webhook 账本行 = %d, want 1", got)
	}
	status := e.scalarString(`
		SELECT d.status || '/' || d.skip_reason FROM notification_deliveries d
		  JOIN notifications n ON n.id = d.notification_id
		 WHERE n.user_id = $1::uuid AND d.channel = 'webhook'`, master.ID)
	if status != corenotify.DeliveryStatusSkipped+"/webhook_not_configured" {
		t.Fatalf("status/reason = %q, want skipped/webhook_not_configured", status)
	}
}

// 两个订阅者同事件均启用 webhook：恰一条真发，另一行 skipped(cooldown_hit)。
func TestNotifyWebhookDeduplicatesEndToEnd(t *testing.T) {
	e := newNotifyDeliveryEnv(t)
	school := e.school("wh-dedupe")
	master := e.admin("wh-dedupe-master@example.com", coreauth.RoleMaster)
	admin := e.admin("wh-dedupe-admin@example.com", coreauth.RoleAdmin, school)
	prefs := webhookOnlyPrefs(corenotify.CategoryCredentialInvalid)
	if err := pg.NewNotificationPrefsRepo(e.db).Replace(e.ctx, master.ID, prefs); err != nil {
		t.Fatalf("replace master prefs: %v", err)
	}
	if err := pg.NewNotificationPrefsRepo(e.db).Replace(e.ctx, admin.ID, prefs); err != nil {
		t.Fatalf("replace admin prefs: %v", err)
	}
	account := e.account(school, master.ID, "13800000002")
	hook := &scriptedHook{}
	clk := &notifyTestClock{now: time.Date(2026, 11, 10, 7, 0, 0, 0, time.UTC)}
	dispatcher := e.webhookDispatcher(hook, clk)

	event := corenotify.Event{
		Category:  corenotify.CategoryCredentialInvalid,
		SchoolID:  school,
		AccountID: account,
		DedupeKey: "credential_invalid/wh-dedupe/1",
		Day:       "2026-11-10",
	}
	if err := dispatcher.Dispatch(e.ctx, event); err != nil {
		t.Fatalf("Dispatch: %v", err)
	}
	if err := dispatcher.SendDue(e.ctx); err != nil {
		t.Fatalf("SendDue: %v", err)
	}
	if hook.sends != 1 {
		t.Fatalf("端口调用 = %d, want 1", hook.sends)
	}
	sent := e.scalarInt(`
		SELECT count(*) FROM notification_deliveries d
		  JOIN notifications n ON n.id = d.notification_id
		 WHERE d.channel = 'webhook' AND d.status = 'sent'`)
	hit := e.scalarInt(`
		SELECT count(*) FROM notification_deliveries d
		  JOIN notifications n ON n.id = d.notification_id
		 WHERE d.channel = 'webhook' AND d.status = 'skipped' AND d.skip_reason = 'cooldown_hit'`)
	if sent != 1 || hit != 1 {
		t.Fatalf("sent=%d cooldown_hit=%d, want 1/1", sent, hit)
	}
}
