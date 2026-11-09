package notify_test

// 摘要的 webhook 账本行判据：按订阅者逐行生成；默认 (sign_failed_daily,
// webhook)=false 不生成；正文即既有摘要正文。

import (
	"context"
	"testing"
	"time"

	appnotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/notify"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
)

// (sign_failed_daily, webhook) 显式开启：摘要为订阅者产出 webhook 账本行，
// 正文与 email 行同源（既有摘要正文）；多订阅者逐行记账。
func TestDispatchDigestWebhookSubscription(t *testing.T) {
	store := newFakeStore()
	source := newFakeDigestSource(
		failedTask("acct-1", "school-a", "13800000001", "blocked", 1, time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)),
	)
	override := map[corenotify.Pair]bool{
		{Category: corenotify.CategorySignFailedDaily, Channel: corenotify.ChannelEmail}:   false,
		{Category: corenotify.CategorySignFailedDaily, Channel: corenotify.ChannelWebhook}: true,
	}
	audience := &fakeDigestAudience{subscribers: []corenotify.DigestSubscriber{
		digestSub("master-1", "m1@example.com", true, nil, override),
		digestSub("admin-1", "a1@example.com", false, []string{"school-a"}, override),
	}}
	hook := &fakeWebhookSender{}
	clk := newManualClock()
	dispatcher, err := appnotify.NewDispatcher(appnotify.DispatcherDeps{
		Notes: store, Ledger: store, Audience: &fakeAudience{},
		Webhook: hook, WebhookCooldown: 0, WebhookDailyMax: 0,
		Location: time.UTC, Clock: clk,
		DigestSource: source, DigestAudience: audience,
	})
	if err != nil {
		t.Fatalf("NewDispatcher: %v", err)
	}
	if err := dispatcher.DispatchDigest(context.Background(), digestDay); err != nil {
		t.Fatalf("DispatchDigest: %v", err)
	}
	rows := 0
	for _, row := range store.deliveryList() {
		if row.Channel == corenotify.ChannelWebhook {
			rows++
			if row.Status != corenotify.DeliveryStatusPending {
				t.Fatalf("webhook 行 status = %s, want pending", row.Status)
			}
		}
	}
	if rows != 2 {
		t.Fatalf("webhook 账本行 = %d, want 2（按订阅者逐行）", rows)
	}
	for _, userID := range []string{"master-1", "admin-1"} {
		note, ok := store.lookupNotification(userID, "sign_failed_daily/"+digestDay)
		if !ok {
			t.Fatalf("收件人 %s 缺摘要通知（锚行）", userID)
		}
		if note.Title != digestDay+" 签到失败摘要" || note.Body == "" {
			t.Fatalf("摘要文本 = %+v", note)
		}
	}
}

// 默认 (sign_failed_daily, webhook)=false：不产出 webhook 账本行。
func TestDispatchDigestWebhookDefaultOff(t *testing.T) {
	store := newFakeStore()
	source := newFakeDigestSource(
		failedTask("acct-1", "school-a", "13800000001", "blocked", 1, time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)),
	)
	audience := &fakeDigestAudience{subscribers: []corenotify.DigestSubscriber{
		digestSub("master-1", "m1@example.com", true, nil, nil),
	}}
	hook := &fakeWebhookSender{}
	clk := newManualClock()
	dispatcher, err := appnotify.NewDispatcher(appnotify.DispatcherDeps{
		Notes: store, Ledger: store, Audience: &fakeAudience{},
		Webhook: hook, WebhookCooldown: 0, WebhookDailyMax: 0,
		Location: time.UTC, Clock: clk,
		DigestSource: source, DigestAudience: audience,
	})
	if err != nil {
		t.Fatalf("NewDispatcher: %v", err)
	}
	if err := dispatcher.DispatchDigest(context.Background(), digestDay); err != nil {
		t.Fatalf("DispatchDigest: %v", err)
	}
	for _, row := range store.deliveryList() {
		if row.Channel == corenotify.ChannelWebhook {
			t.Fatalf("默认关闭的 webhook 渠道仍产出账本行: %+v", row)
		}
	}
}
