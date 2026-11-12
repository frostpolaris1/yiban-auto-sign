package notify_test

// 摘要的 webhook 账本行判据：按订阅者逐行生成；默认 (sign_failed_daily,
// webhook)=false 不生成；正文即既有摘要正文。

import (
	"context"
	"strings"
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

// P2-4：摘要 webhook 按订阅者各自 scope 送达。
// master 与 adminA 绑定不同校：两条 webhook 行正文各自按 scope 聚合；
// 同题节流只折叠**同文**推送——不同 scope 正文互不折叠，
// 旧口径「首个收件人独占、其余 skipped(cooldown_hit)」不再成立。
func TestDispatchDigestWebhookPerRecipientScope(t *testing.T) {
	store := newFakeStore()
	source := newFakeDigestSource(
		failedTask("acct-a", "school-a", "13800000001", "blocked", 1, time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)),
		failedTask("acct-b", "school-b", "13800000002", "auth_failed", 2, time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)),
	)
	override := map[corenotify.Pair]bool{
		{Category: corenotify.CategorySignFailedDaily, Channel: corenotify.ChannelEmail}:   false,
		{Category: corenotify.CategorySignFailedDaily, Channel: corenotify.ChannelWebhook}: true,
	}
	audience := &fakeDigestAudience{subscribers: []corenotify.DigestSubscriber{
		digestSub("master-1", "m1@example.com", true, nil, override),
		digestSub("admin-1", "a1@example.com", false, []string{"school-b"}, override),
	}}
	hook := &fakeWebhookSender{}
	clk := newManualClock()
	dispatcher, err := appnotify.NewDispatcher(appnotify.DispatcherDeps{
		Notes: store, Ledger: store, Audience: &fakeAudience{},
		Webhook: hook, WebhookCooldown: time.Minute, WebhookDailyMax: 0,
		Location: time.UTC, Clock: clk,
		DigestSource: source, DigestAudience: audience,
	})
	if err != nil {
		t.Fatalf("NewDispatcher: %v", err)
	}
	if err := dispatcher.DispatchDigest(context.Background(), digestDay); err != nil {
		t.Fatalf("DispatchDigest: %v", err)
	}
	if err := dispatcher.SendDue(context.Background()); err != nil {
		t.Fatalf("SendDue: %v", err)
	}
	pushes := hook.received()
	if len(pushes) != 2 {
		t.Fatalf("webhook 送达 = %d, want 2（每订阅者各自 scope 正文）", len(pushes))
	}
	master, admin := pushes[0], pushes[1]
	// master 全量：两校都出现；admin 只见绑定校。
	if !strings.Contains(master.Content, "138****0001") || !strings.Contains(master.Content, "138****0002") {
		t.Fatalf("master 正文 = %q, want 两校都在", master.Content)
	}
	if strings.Contains(admin.Content, "138****0001") || !strings.Contains(admin.Content, "138****0002") {
		t.Fatalf("admin 正文 = %q, want 仅绑定校", admin.Content)
	}
	// 送达顺序按入账顺序：master 先入账先送达（断言不钉发送序细节，只钉两条都在）。
	for _, row := range store.deliveryList() {
		if row.Status != corenotify.DeliveryStatusSent {
			t.Fatalf("webhook 行 = %+v, want sent（同文折叠只该命中相同正文）", row)
		}
	}

	// 同文折叠仍在：同 scope 的两订阅者（正文相同）仍恰一条真发。
	store2 := newFakeStore()
	audience2 := &fakeDigestAudience{subscribers: []corenotify.DigestSubscriber{
		digestSub("master-1", "m1@example.com", true, nil, override),
		digestSub("admin-2", "a2@example.com", false, []string{"school-a", "school-b"}, override),
	}}
	hook2 := &fakeWebhookSender{}
	dispatcher2, err := appnotify.NewDispatcher(appnotify.DispatcherDeps{
		Notes: store2, Ledger: store2, Audience: &fakeAudience{},
		Webhook: hook2, WebhookCooldown: time.Minute, WebhookDailyMax: 0,
		Location: time.UTC, Clock: clk,
		DigestSource: source, DigestAudience: audience2,
	})
	if err != nil {
		t.Fatalf("NewDispatcher 2: %v", err)
	}
	if err := dispatcher2.DispatchDigest(context.Background(), digestDay); err != nil {
		t.Fatalf("DispatchDigest 2: %v", err)
	}
	if err := dispatcher2.SendDue(context.Background()); err != nil {
		t.Fatalf("SendDue 2: %v", err)
	}
	if hook2.calls() != 1 {
		t.Fatalf("同文推送 = %d, want 1（相同 scope 正文仍全局折叠）", hook2.calls())
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
