package notify_test

// 2d webhook 渠道的投递用例判据：账本按订阅者逐行记账、
// 发送期同题节流与当日额度、端口未配置与退避沿用既有规则。
//
// 覆盖：仅 webhook 订阅启用时的锚行与账本行；端口 nil 立即
// skipped(webhook_not_configured)；多订阅者同事件恰一条真发、
// 其余 skipped(cooldown_hit)；额度用尽 skipped(budget_exhausted)、
// failed/skipped 不计数、0 不限额；冷却 0 不节流；
// 日志与错误不含出口凭据。

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	appnotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/notify"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
	corewebhook "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/webhook"
)

// fakeWebhookSender 按脚本返回结果，并记录收到的推送。
type fakeWebhookSender struct {
	mu       sync.Mutex
	pushes   []corewebhook.Push
	results  []error
	panicNow bool
}

func (s *fakeWebhookSender) Send(_ context.Context, push corewebhook.Push) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.panicNow {
		panic("webhook sender panic")
	}
	s.pushes = append(s.pushes, push)
	if len(s.results) == 0 {
		return nil
	}
	err := s.results[0]
	s.results = s.results[1:]
	return err
}

func (s *fakeWebhookSender) calls() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.pushes)
}

// newWebhookDelivery 构造带 webhook 端口的投递服务。
func newWebhookDelivery(t *testing.T, store *fakeStore, audience *fakeAudience,
	hook corewebhook.Sender, cooldown time.Duration, dailyMax int,
	clk *manualClock, logs *bytes.Buffer) *appnotify.Dispatcher {
	t.Helper()
	var logger *slog.Logger
	if logs != nil {
		logger = slog.New(slog.NewTextHandler(logs, nil))
	}
	dispatcher, err := appnotify.NewDispatcher(appnotify.DispatcherDeps{
		Notes: store, Ledger: store, Audience: audience,
		Webhook: hook, WebhookCooldown: cooldown, WebhookDailyMax: dailyMax,
		Location: time.UTC, Clock: clk, Logger: logger,
	})
	if err != nil {
		t.Fatalf("NewDispatcher: %v", err)
	}
	return dispatcher
}

// webhookOnly 覆盖：只开 webhook 的订阅。
// email 显式关掉：review_pending 的 email 默认开启，不关会多出账本行。
func webhookOnly(category corenotify.Category) map[corenotify.Pair]bool {
	return map[corenotify.Pair]bool{
		{Category: category, Channel: corenotify.ChannelEmail}:   false,
		{Category: category, Channel: corenotify.ChannelWebhook}: true,
	}
}

func channelStatuses(store *fakeStore, channel corenotify.Channel) map[string]string {
	out := map[string]string{}
	for _, row := range store.deliveryList() {
		if row.Channel == channel {
			out[row.UserID] = row.Status
		}
	}
	return out
}

// 仅 webhook 订阅启用（in_app/email 关）：站内信锚行存在；
// 端口 nil 时账本行立即 skipped(webhook_not_configured)。
func TestDispatchWebhookOnlyWithoutOutlet(t *testing.T) {
	store := newFakeStore()
	clk := newManualClock()
	audience := &fakeAudience{
		subscribers: []corenotify.Subscriber{overriding("master-1", "m@example.com", webhookOnly(corenotify.CategoryCredentialInvalid))},
		phones:      map[string]string{"acct-1": "13800000001"},
	}
	dispatcher := newWebhookDelivery(t, store, audience, nil, time.Minute, 5, clk, nil)

	if err := dispatcher.Dispatch(context.Background(), credentialEvent("acct-1")); err != nil {
		t.Fatalf("Dispatch: %v", err)
	}
	// 锚行存在：任一渠道启用即写站内信。
	if notes := store.notificationList(); len(notes) != 1 {
		t.Fatalf("站内信锚行 = %d, want 1", len(notes))
	}
	rows := store.deliveryList()
	if len(rows) != 1 || rows[0].Channel != corenotify.ChannelWebhook {
		t.Fatalf("账本行 = %+v, want one webhook row", rows)
	}
	if rows[0].Status != corenotify.DeliveryStatusSkipped ||
		rows[0].SkipReason != "webhook_not_configured" {
		t.Fatalf("status/reason = %s/%s, want skipped/webhook_not_configured",
			rows[0].Status, rows[0].SkipReason)
	}
}

// 端口注入时 webhook 账本行保持 pending，等 SendDue 发送。
func TestDispatchWebhookOnlyWithOutletKeepsPending(t *testing.T) {
	store := newFakeStore()
	clk := newManualClock()
	audience := &fakeAudience{
		subscribers: []corenotify.Subscriber{overriding("master-1", "m@example.com", webhookOnly(corenotify.CategoryReviewPending))},
		phones:      map[string]string{"acct-1": "13800000001"},
	}
	dispatcher := newWebhookDelivery(t, store, audience, &fakeWebhookSender{}, time.Minute, 5, clk, nil)

	if err := dispatcher.Dispatch(context.Background(), corenotify.Event{
		Category:  corenotify.CategoryReviewPending,
		SchoolID:  "school-1",
		AccountID: "acct-1",
		DedupeKey: "review_pending/acct-1/2026-11-10",
		Day:       "2026-11-10",
	}); err != nil {
		t.Fatalf("Dispatch: %v", err)
	}
	rows := store.deliveryList()
	if len(rows) != 1 || rows[0].Channel != corenotify.ChannelWebhook ||
		rows[0].Status != corenotify.DeliveryStatusPending {
		t.Fatalf("账本行 = %+v, want pending webhook row", rows)
	}
}

// 两个订阅者同事件均启用 webhook：恰一条真发（端口调用计数 == 1），
// 另一行 skipped(cooldown_hit)。多管理员共享一个出口，账本逐行诚实记账。
func TestSendDueWebhookDeduplicatesByTitle(t *testing.T) {
	store := newFakeStore()
	clk := newManualClock()
	audience := &fakeAudience{
		subscribers: []corenotify.Subscriber{
			overriding("master-1", "m1@example.com", webhookOnly(corenotify.CategoryCredentialInvalid)),
			overriding("admin-1", "a1@example.com", webhookOnly(corenotify.CategoryCredentialInvalid)),
		},
		phones: map[string]string{"acct-1": "13800000001"},
	}
	hook := &fakeWebhookSender{}
	dispatcher := newWebhookDelivery(t, store, audience, hook, time.Minute, 5, clk, nil)

	if err := dispatcher.Dispatch(context.Background(), credentialEvent("acct-1")); err != nil {
		t.Fatalf("Dispatch: %v", err)
	}
	if err := dispatcher.SendDue(context.Background()); err != nil {
		t.Fatalf("SendDue: %v", err)
	}
	if hook.calls() != 1 {
		t.Fatalf("端口调用 = %d, want 1", hook.calls())
	}
	statuses := channelStatuses(store, corenotify.ChannelWebhook)
	if len(statuses) != 2 {
		t.Fatalf("webhook 行 = %d, want 2（按订阅者逐行记账）", len(statuses))
	}
	sent, hit := 0, 0
	for _, status := range statuses {
		switch status {
		case corenotify.DeliveryStatusSent:
			sent++
		case corenotify.DeliveryStatusSkipped:
			hit++
		}
	}
	if sent != 1 || hit != 1 {
		t.Fatalf("sent=%d skipped=%d, want 1/1", sent, hit)
	}
	// 跳过原因确实是冷却命中。
	for _, row := range store.deliveryList() {
		if row.Status == corenotify.DeliveryStatusSkipped && row.SkipReason != "cooldown_hit" {
			t.Fatalf("skip reason = %q, want cooldown_hit", row.SkipReason)
		}
	}
}

// 冷却窗口内同题第二次 → cooldown_hit；COOLDOWN_SEC=0 → 不节流。
func TestWebhookCooldownWindow(t *testing.T) {
	t.Run("window blocks second push", func(t *testing.T) {
		store := newFakeStore()
		clk := newManualClock()
		audience := &fakeAudience{
			subscribers: []corenotify.Subscriber{overriding("master-1", "m@example.com", webhookOnly(corenotify.CategoryCredentialInvalid))},
			phones:      map[string]string{"acct-1": "13800000001", "acct-2": "13800000002"},
		}
		hook := &fakeWebhookSender{}
		dispatcher := newWebhookDelivery(t, store, audience, hook, time.Minute, 0, clk, nil)
		if err := dispatcher.Dispatch(context.Background(), credentialEvent("acct-1")); err != nil {
			t.Fatalf("Dispatch: %v", err)
		}
		if err := dispatcher.SendDue(context.Background()); err != nil {
			t.Fatalf("SendDue: %v", err)
		}
		// 同题第二次：节流窗口内。
		if err := dispatcher.Dispatch(context.Background(), credentialEvent("acct-2")); err != nil {
			t.Fatalf("Dispatch: %v", err)
		}
		if err := dispatcher.SendDue(context.Background()); err != nil {
			t.Fatalf("SendDue: %v", err)
		}
		if hook.calls() != 1 {
			t.Fatalf("端口调用 = %d, want 1", hook.calls())
		}
	})
	t.Run("zero cooldown disables throttle", func(t *testing.T) {
		store := newFakeStore()
		clk := newManualClock()
		audience := &fakeAudience{
			subscribers: []corenotify.Subscriber{overriding("master-1", "m@example.com", webhookOnly(corenotify.CategoryCredentialInvalid))},
			phones:      map[string]string{"acct-1": "13800000001", "acct-2": "13800000002"},
		}
		hook := &fakeWebhookSender{}
		dispatcher := newWebhookDelivery(t, store, audience, hook, 0, 0, clk, nil)
		if err := dispatcher.Dispatch(context.Background(), credentialEvent("acct-1")); err != nil {
			t.Fatalf("Dispatch: %v", err)
		}
		if err := dispatcher.SendDue(context.Background()); err != nil {
			t.Fatalf("SendDue: %v", err)
		}
		if err := dispatcher.Dispatch(context.Background(), credentialEvent("acct-2")); err != nil {
			t.Fatalf("Dispatch: %v", err)
		}
		if err := dispatcher.SendDue(context.Background()); err != nil {
			t.Fatalf("SendDue: %v", err)
		}
		if hook.calls() != 2 {
			t.Fatalf("端口调用 = %d, want 2（节流关闭）", hook.calls())
		}
	})
}

// 当日额度：sent 行数达 DAILY_MAX → skipped(budget_exhausted)；
// failed/skipped 行不计数；DAILY_MAX=0 → 不限。
func TestWebhookDailyBudget(t *testing.T) {
	audience := func() *fakeAudience {
		return &fakeAudience{
			subscribers: []corenotify.Subscriber{overriding("master-1", "m@example.com", webhookOnly(corenotify.CategoryCredentialInvalid))},
			phones:      map[string]string{"acct-1": "13800000001"},
		}
	}
	t.Run("budget exhausted", func(t *testing.T) {
		store := newFakeStore()
		clk := newManualClock()
		// 手工造一条当日 sent webhook 行：额度已被占满。
		note, err := store.Insert(context.Background(), corenotify.NotificationInsert{
			UserID: "other", Category: corenotify.CategoryReviewPending,
			DedupeKey: "seed", Title: "t", Body: "b", CreatedAt: clk.Now(),
		})
		if err != nil {
			t.Fatalf("seed note: %v", err)
		}
		row, err := store.InsertPending(context.Background(), corenotify.DeliveryInsert{
			NotificationID: note.ID, UserID: "other",
			Channel: corenotify.ChannelWebhook, NextRetryAt: &[]time.Time{clk.Now()}[0],
		})
		if err != nil {
			t.Fatalf("seed row: %v", err)
		}
		store.clock = clk
		if err := store.MarkSent(context.Background(), row.ID); err != nil {
			t.Fatalf("seed sent: %v", err)
		}
		hook := &fakeWebhookSender{}
		dispatcher := newWebhookDelivery(t, store, audience(), hook, time.Minute, 1, clk, nil)
		if err := dispatcher.Dispatch(context.Background(), credentialEvent("acct-1")); err != nil {
			t.Fatalf("Dispatch: %v", err)
		}
		if err := dispatcher.SendDue(context.Background()); err != nil {
			t.Fatalf("SendDue: %v", err)
		}
		if hook.calls() != 0 {
			t.Fatalf("端口调用 = %d, want 0（额度已尽）", hook.calls())
		}
		for _, row := range store.deliveryList() {
			if row.UserID == "master-1" &&
				(row.Status != corenotify.DeliveryStatusSkipped || row.SkipReason != "budget_exhausted") {
				t.Fatalf("status/reason = %s/%s, want skipped/budget_exhausted",
					row.Status, row.SkipReason)
			}
		}
	})
	t.Run("failed rows do not count", func(t *testing.T) {
		store := newFakeStore()
		clk := newManualClock()
		note, err := store.Insert(context.Background(), corenotify.NotificationInsert{
			UserID: "other", Category: corenotify.CategoryReviewPending,
			DedupeKey: "seed", Title: "t", Body: "b", CreatedAt: clk.Now(),
		})
		if err != nil {
			t.Fatalf("seed note: %v", err)
		}
		row, err := store.InsertPending(context.Background(), corenotify.DeliveryInsert{
			NotificationID: note.ID, UserID: "other",
			Channel: corenotify.ChannelWebhook, NextRetryAt: &[]time.Time{clk.Now()}[0],
		})
		if err != nil {
			t.Fatalf("seed row: %v", err)
		}
		store.clock = clk
		// 失败一次：终态 failed，不占额度。
		if err := store.MarkFailed(context.Background(), row.ID, string(corewebhook.KindRefused), nil); err != nil {
			t.Fatalf("seed failed: %v", err)
		}
		hook := &fakeWebhookSender{}
		dispatcher := newWebhookDelivery(t, store, audience(), hook, time.Minute, 1, clk, nil)
		if err := dispatcher.Dispatch(context.Background(), credentialEvent("acct-1")); err != nil {
			t.Fatalf("Dispatch: %v", err)
		}
		if err := dispatcher.SendDue(context.Background()); err != nil {
			t.Fatalf("SendDue: %v", err)
		}
		if hook.calls() != 1 {
			t.Fatalf("端口调用 = %d, want 1（failed 行不占额度）", hook.calls())
		}
	})
	t.Run("zero max is unlimited", func(t *testing.T) {
		store := newFakeStore()
		clk := newManualClock()
		// 造 3 条当日 sent 行：max=0 不限额，仍放行。
		for i := 0; i < 3; i++ {
			note, err := store.Insert(context.Background(), corenotify.NotificationInsert{
				UserID: "other", Category: corenotify.CategoryReviewPending,
				DedupeKey: "seed-" + string(rune('a'+i)), Title: "t", Body: "b", CreatedAt: clk.Now(),
			})
			if err != nil {
				t.Fatalf("seed note: %v", err)
			}
			row, err := store.InsertPending(context.Background(), corenotify.DeliveryInsert{
				NotificationID: note.ID, UserID: "other",
				Channel: corenotify.ChannelWebhook, NextRetryAt: &[]time.Time{clk.Now()}[0],
			})
			if err != nil {
				t.Fatalf("seed row: %v", err)
			}
			store.clock = clk
			if err := store.MarkSent(context.Background(), row.ID); err != nil {
				t.Fatalf("seed sent: %v", err)
			}
		}
		hook := &fakeWebhookSender{}
		dispatcher := newWebhookDelivery(t, store, audience(), hook, time.Minute, 0, clk, nil)
		if err := dispatcher.Dispatch(context.Background(), credentialEvent("acct-1")); err != nil {
			t.Fatalf("Dispatch: %v", err)
		}
		if err := dispatcher.SendDue(context.Background()); err != nil {
			t.Fatalf("SendDue: %v", err)
		}
		if hook.calls() != 1 {
			t.Fatalf("端口调用 = %d, want 1（0=不限额）", hook.calls())
		}
	})
}

// webhook 失败按 core/webhook 分类走既有退避；不可重试立即 failed；
// 失败释放节流占位：重试不在窗口内被自己静音。
func TestWebhookFailureRetryAndRelease(t *testing.T) {
	store := newFakeStore()
	clk := newManualClock()
	audience := &fakeAudience{
		subscribers: []corenotify.Subscriber{overriding("master-1", "m@example.com", webhookOnly(corenotify.CategoryCredentialInvalid))},
		phones:      map[string]string{"acct-1": "13800000001"},
	}
	hook := &fakeWebhookSender{results: []error{
		&corewebhook.DeliveryError{Kind: corewebhook.KindTimeout, Retryable: true,
			Err: errors.New("deadline")},
	}}
	dispatcher := newWebhookDelivery(t, store, audience, hook, time.Minute, 5, clk, nil)
	if err := dispatcher.Dispatch(context.Background(), credentialEvent("acct-1")); err != nil {
		t.Fatalf("Dispatch: %v", err)
	}
	if err := dispatcher.SendDue(context.Background()); err != nil {
		t.Fatalf("SendDue: %v", err)
	}
	rows := store.deliveryList()
	if rows[0].Status != corenotify.DeliveryStatusPending ||
		rows[0].LastErrorKind != string(corewebhook.KindTimeout) {
		t.Fatalf("status/kind = %s/%s, want pending/timeout", rows[0].Status, rows[0].LastErrorKind)
	}
	if rows[0].NextRetryAt == nil || !rows[0].NextRetryAt.Equal(clk.Now().Add(time.Minute)) {
		t.Fatalf("next retry = %v, want +1m", rows[0].NextRetryAt)
	}
	// 推进到重试时刻再发：失败释放了节流占位，这次应真发。
	clk.Advance(time.Minute)
	hook.results = nil
	if err := dispatcher.SendDue(context.Background()); err != nil {
		t.Fatalf("SendDue retry: %v", err)
	}
	if hook.calls() != 2 {
		t.Fatalf("端口调用 = %d, want 2（失败释放节流）", hook.calls())
	}
	rows = store.deliveryList()
	if rows[0].Status != corenotify.DeliveryStatusSent {
		t.Fatalf("重试后 status = %s, want sent", rows[0].Status)
	}
}

// webhook 失败日志不含出口凭据（出口凭据不进 Dispatcher 日志面）。
func TestWebhookFailureLogHoldsNoSecret(t *testing.T) {
	store := newFakeStore()
	clk := newManualClock()
	audience := &fakeAudience{
		subscribers: []corenotify.Subscriber{overriding("master-1", "m@example.com", webhookOnly(corenotify.CategoryCredentialInvalid))},
		phones:      map[string]string{"acct-1": "13800000001"},
	}
	const secret = "SCT_FAKE_TEST_KEY_000000"
	hook := &fakeWebhookSender{results: []error{
		&corewebhook.DeliveryError{Kind: corewebhook.KindRefused,
			Err: errors.New("https://sctapi.ftqq.com/" + secret + ".send refused")},
	}}
	logs := &bytes.Buffer{}
	dispatcher := newWebhookDelivery(t, store, audience, hook, time.Minute, 5, clk, logs)
	if err := dispatcher.Dispatch(context.Background(), credentialEvent("acct-1")); err != nil {
		t.Fatalf("Dispatch: %v", err)
	}
	if err := dispatcher.SendDue(context.Background()); err != nil {
		t.Fatalf("SendDue: %v", err)
	}
	if strings.Contains(logs.String(), secret) {
		t.Fatalf("日志泄漏出口凭据: %s", logs.String())
	}
}
