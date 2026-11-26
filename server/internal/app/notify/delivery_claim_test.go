package notify_test

// P3-9/P3-11④ 投递认领租约的判据。
//
// 覆盖：SendDue 只投认领到的行（他实例租约内的行同轮不可见）；
// 崩溃窗口（信已发、账本未落）内租约挡住第二轮，租约到期后重投
//（投递是至少一次）；认领失败原样上抛；单行收件人复核失败不挡其余行；
// sent/failed/skipped 三种终态记账都清空认领。

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	appnotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/notify"
	coremail "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mail"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
)

// reviewAudience 只回一个显式开 email、关 webhook 的 master 收件人。
func reviewAudience() *fakeAudience {
	return &fakeAudience{
		subscribers: []corenotify.Subscriber{
			overriding("master-1", "master-1@example.com", map[corenotify.Pair]bool{
				{Category: corenotify.CategoryReviewPending, Channel: corenotify.ChannelEmail}:   true,
				{Category: corenotify.CategoryReviewPending, Channel: corenotify.ChannelWebhook}: false,
			}),
		},
		phones: map[string]string{"acct-1": "13800000001"},
	}
}

// dispatchReview 造一条 review_pending 事件并落一条 email 账本行。
func dispatchReview(t *testing.T, d *appnotify.Dispatcher, store *fakeStore) {
	t.Helper()
	if err := d.Dispatch(context.Background(), corenotify.Event{
		Category:  corenotify.CategoryReviewPending,
		SchoolID:  "school-1",
		AccountID: "acct-1",
		DedupeKey: "review_pending/acct-1/evt-claim",
	}); err != nil {
		t.Fatalf("Dispatch: %v", err)
	}
	if got := len(store.deliveryList()); got != 1 {
		t.Fatalf("账本行 = %d, want 1", got)
	}
}

// newLedgerDispatcher 用给定账本装饰器构造第二个实例。
func newLedgerDispatcher(t *testing.T, store *fakeStore, ledger appnotify.DeliveryLedger,
	audience *fakeAudience, mail coremail.Sender, clk *manualClock) *appnotify.Dispatcher {
	t.Helper()
	d, err := appnotify.NewDispatcher(appnotify.DispatcherDeps{
		Notes: store, Ledger: ledger, Audience: audience, Mail: mail, Clock: clk,
	})
	if err != nil {
		t.Fatalf("NewDispatcher: %v", err)
	}
	return d
}

// droppedMarkSent 丢掉 MarkSent：模拟「信已发出、账本还没写」的崩溃窗口。
type droppedMarkSent struct {
	appnotify.DeliveryLedger
}

func (droppedMarkSent) MarkSent(context.Context, string) error { return nil }

// 他实例已认领的行在租约内不投；租约到期后才可见。
func TestSendDueSkipsRowsClaimedByAnotherInstance(t *testing.T) {
	store := newFakeStore()
	clk := newManualClock()
	audience := reviewAudience()
	store.emails["master-1"] = "master-1@example.com"
	sender := &fakeSender{}
	dispatcher := newDelivery(t, store, audience, sender, clk, nil)
	dispatchReview(t, dispatcher, store)

	held, err := store.ClaimDueForSend(context.Background(), clk.Now(), 10, time.Minute)
	if err != nil || len(held) != 1 {
		t.Fatalf("预认领 = (%d,%v), want 1", len(held), err)
	}

	if err := dispatcher.SendDue(context.Background()); err != nil {
		t.Fatalf("SendDue: %v", err)
	}
	if got := sender.calls(); got != 0 {
		t.Fatalf("租约内仍投递: calls=%d, want 0（SendDue 必须走认领）", got)
	}

	clk.Advance(time.Minute + time.Second)
	if err := dispatcher.SendDue(context.Background()); err != nil {
		t.Fatalf("SendDue: %v", err)
	}
	if got := sender.calls(); got != 1 {
		t.Fatalf("租约到期后 calls=%d, want 1", got)
	}
	if lease := store.claimLease; lease != 15*time.Minute {
		t.Fatalf("SendDue 租约 = %s, want 15m", lease)
	}
}

// 崩溃窗口：MarkSent 丢失时行保持 pending 且带租约；租约内第二轮不得重投，
// 租约到期后重投 —— 投递是至少一次。
func TestSendDueLeaseBoundsCrashWindow(t *testing.T) {
	store := newFakeStore()
	clk := newManualClock()
	audience := reviewAudience()
	store.emails["master-1"] = "master-1@example.com"
	sender := &fakeSender{}
	dispatcher := newDelivery(t, store, audience, sender, clk, nil)
	dispatchReview(t, dispatcher, store)

	crashed := newLedgerDispatcher(t, store, droppedMarkSent{store}, audience, sender, clk)
	if err := crashed.SendDue(context.Background()); err != nil {
		t.Fatalf("SendDue: %v", err)
	}
	if got := sender.calls(); got != 1 {
		t.Fatalf("首轮 calls=%d, want 1", got)
	}

	// 第二轮：旧实现没有认领，会把同一 pending 行再发一次（calls=2）。
	if err := dispatcher.SendDue(context.Background()); err != nil {
		t.Fatalf("SendDue: %v", err)
	}
	if got := sender.calls(); got != 1 {
		t.Fatalf("租约内重投: calls=%d, want 1", got)
	}
	rows := store.deliveryList()
	if rows[0].Status != corenotify.DeliveryStatusPending {
		t.Fatalf("崩溃窗口后状态 = %s, want pending", rows[0].Status)
	}
	if !store.claimPresent(rows[0].ID) {
		t.Fatalf("崩溃窗口后缺认领租约")
	}

	clk.Advance(15*time.Minute + time.Second)
	if err := dispatcher.SendDue(context.Background()); err != nil {
		t.Fatalf("SendDue: %v", err)
	}
	if got := sender.calls(); got != 2 {
		t.Fatalf("租约到期后 calls=%d, want 2（至少一次）", got)
	}
}

// 认领失败原样上抛，且不投递任何行。
func TestSendDuePropagatesClaimError(t *testing.T) {
	store := newFakeStore()
	clk := newManualClock()
	audience := reviewAudience()
	store.emails["master-1"] = "master-1@example.com"
	sender := &fakeSender{}
	dispatcher := newDelivery(t, store, audience, sender, clk, nil)
	dispatchReview(t, dispatcher, store)

	claimErr := errors.New("claim store unavailable")
	store.failClaim = claimErr
	err := dispatcher.SendDue(context.Background())
	if !errors.Is(err, claimErr) {
		t.Fatalf("SendDue error = %v, want 原样上抛 %v", err, claimErr)
	}
	if got := sender.calls(); got != 0 {
		t.Fatalf("认领失败仍投递: calls=%d", got)
	}
}

// 单行投递错误不挡其余行：第一行收件人复核失败，第二行照常投。
func TestSendDueContinuesAfterRowDeliverError(t *testing.T) {
	store := newFakeStore()
	clk := newManualClock()
	store.emails["user-err"] = "err@example.com"
	store.emails["user-ok"] = "ok@example.com"
	past := clk.Now().Add(-time.Minute)
	ctx := context.Background()
	for i, userID := range []string{"user-err", "user-ok"} {
		note, err := store.Insert(ctx, corenotify.NotificationInsert{
			UserID:    userID,
			Category:  corenotify.CategoryReviewPending,
			DedupeKey: fmt.Sprintf("multi/%d", i),
			Title:     "账号待审核",
			Body:      "body",
			CreatedAt: clk.Now(),
		})
		if err != nil {
			t.Fatalf("Insert: %v", err)
		}
		if _, err := store.InsertPending(ctx, corenotify.DeliveryInsert{
			NotificationID: note.ID,
			UserID:         userID,
			Channel:        corenotify.ChannelEmail,
			NextRetryAt:    &past,
		}); err != nil {
			t.Fatalf("InsertPending: %v", err)
		}
	}
	audience := &fakeAudience{activeErr: map[string]error{"user-err": errors.New("audience read failed")}}
	sender := &fakeSender{}
	dispatcher := newDelivery(t, store, audience, sender, clk, nil)

	err := dispatcher.SendDue(ctx)
	if err == nil || !strings.Contains(err.Error(), "audience read failed") {
		t.Fatalf("SendDue error = %v, want 第一条错误", err)
	}
	if got := sender.calls(); got != 1 {
		t.Fatalf("其余行未继续: calls=%d, want 1", got)
	}
	for _, row := range store.deliveryList() {
		switch row.UserID {
		case "user-ok":
			if row.Status != corenotify.DeliveryStatusSent {
				t.Fatalf("ok 行状态 = %s, want sent", row.Status)
			}
		case "user-err":
			if row.Status != corenotify.DeliveryStatusPending {
				t.Fatalf("err 行状态 = %s, want pending", row.Status)
			}
		}
	}
}

// 收件人复核读失败：必须在返回前释放认领，同一 now 的下一轮就能重新认领，
// 而不是被 claimed_until 藏满 15 分钟。
func TestSendDueReleasesClaimOnAudienceReadError(t *testing.T) {
	store := newFakeStore()
	clk := newManualClock()
	audience := reviewAudience()
	store.emails["master-1"] = "master-1@example.com"
	sender := &fakeSender{}
	dispatcher := newDelivery(t, store, audience, sender, clk, nil)
	dispatchReview(t, dispatcher, store)
	row := store.deliveryList()[0]

	audience.activeErr = map[string]error{"master-1": errors.New("audience read failed")}
	ctx := context.Background()
	err := dispatcher.SendDue(ctx)
	if err == nil || !strings.Contains(err.Error(), "audience read failed") {
		t.Fatalf("SendDue error = %v, want 原样上抛读错误", err)
	}
	if got := store.releaseCount(); got != 1 {
		t.Fatalf("读错误后释放 = %d 次, want 1（不释放会等满 15m 租约）", got)
	}
	if store.claimPresent(row.ID) {
		t.Fatalf("读错误后仍持租约: 下一轮 30s 内认领不到")
	}

	// 同一 now 立即再跑：释放后的行必须能被重新认领。
	err = dispatcher.SendDue(ctx)
	if err == nil || !strings.Contains(err.Error(), "audience read failed") {
		t.Fatalf("第二轮 SendDue error = %v, want 同一读错误", err)
	}
	if got := store.releaseCount(); got != 2 {
		t.Fatalf("第二轮释放累计 = %d, want 2（行必须能重新认领）", got)
	}
	if got := sender.calls(); got != 0 {
		t.Fatalf("读错误却发送: calls=%d, want 0", got)
	}
}

// 释放本身失败只 Warn，不覆盖原始的读错误（行仍被租约藏着，最多等满租约）。
func TestSendDueReleaseFailureKeepsOriginalError(t *testing.T) {
	store := newFakeStore()
	clk := newManualClock()
	audience := reviewAudience()
	store.emails["master-1"] = "master-1@example.com"
	sender := &fakeSender{}
	dispatcher := newDelivery(t, store, audience, sender, clk, nil)
	dispatchReview(t, dispatcher, store)

	audience.activeErr = map[string]error{"master-1": errors.New("audience read failed")}
	store.failRelease = errors.New("release failed")
	err := dispatcher.SendDue(context.Background())
	if err == nil || !strings.Contains(err.Error(), "audience read failed") {
		t.Fatalf("SendDue error = %v, want 原始读错误", err)
	}
	if strings.Contains(err.Error(), "release failed") {
		t.Fatalf("SendDue error = %v, 释放失败不应覆盖原始错误", err)
	}
	if got := store.releaseCount(); got != 1 {
		t.Fatalf("释放尝试 = %d 次, want 1", got)
	}
	if got := sender.calls(); got != 0 {
		t.Fatalf("读错误却发送: calls=%d, want 0", got)
	}
}

// webhook 额度读失败：同样在返回前释放认领，下一轮即可重试。
func TestSendDueReleasesClaimOnBudgetReadError(t *testing.T) {
	store := newFakeStore()
	clk := newManualClock()
	audience := &fakeAudience{
		subscribers: []corenotify.Subscriber{
			overriding("master-1", "master-1@example.com", webhookOnly(corenotify.CategoryCredentialInvalid)),
		},
		phones: map[string]string{"acct-1": "13800000001"},
	}
	hook := &fakeWebhookSender{}
	dispatcher := newWebhookDelivery(t, store, audience, hook, 0, 5, clk, nil)
	ctx := context.Background()
	if err := dispatcher.Dispatch(ctx, credentialEvent("acct-1")); err != nil {
		t.Fatalf("Dispatch: %v", err)
	}
	row := store.deliveryList()[0]

	store.countSentErr = errors.New("budget store unavailable")
	err := dispatcher.SendDue(ctx)
	if err == nil || !strings.Contains(err.Error(), "budget store unavailable") {
		t.Fatalf("SendDue error = %v, want 原样上抛读错误", err)
	}
	if got := store.releaseCount(); got != 1 {
		t.Fatalf("额度读错误后释放 = %d 次, want 1", got)
	}
	if store.claimPresent(row.ID) {
		t.Fatalf("额度读错误后仍持租约: 下一轮 30s 内认领不到")
	}

	err = dispatcher.SendDue(ctx)
	if err == nil || !strings.Contains(err.Error(), "budget store unavailable") {
		t.Fatalf("第二轮 SendDue error = %v, want 同一读错误", err)
	}
	if got := store.releaseCount(); got != 2 {
		t.Fatalf("第二轮释放累计 = %d, want 2（行必须能重新认领）", got)
	}
	if got := hook.calls(); got != 0 {
		t.Fatalf("额度读失败却推送: calls=%d, want 0", got)
	}
}

// 三种终态记账都清空认领。
// fake 钉住 app 侧语义（终态记账后租约消失）；SQL 里 claimed_until=NULL 的
// 清理由 pg e2e TestClaimDueClearedByTerminalMarks 钉住。
func TestTerminalMarksClearClaim(t *testing.T) {
	now := time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)
	past := now.Add(-time.Minute)
	cases := []struct {
		name string
		mark func(*fakeStore, string) error
	}{
		{"sent", func(s *fakeStore, id string) error { return s.MarkSent(context.Background(), id) }},
		{"failed", func(s *fakeStore, id string) error {
			return s.MarkFailed(context.Background(), id, "connect", nil)
		}},
		{"skipped", func(s *fakeStore, id string) error {
			return s.MarkSkipped(context.Background(), id, "cooldown_hit")
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			store := newFakeStore()
			ctx := context.Background()
			delivery, err := store.InsertPending(ctx, corenotify.DeliveryInsert{
				NotificationID: "notification-claim",
				UserID:         "user-claim",
				Channel:        corenotify.ChannelEmail,
				NextRetryAt:    &past,
			})
			if err != nil {
				t.Fatalf("InsertPending: %v", err)
			}
			claimed, err := store.ClaimDueForSend(ctx, now, 10, time.Minute)
			if err != nil || len(claimed) != 1 {
				t.Fatalf("ClaimDueForSend = (%d,%v)", len(claimed), err)
			}
			if !store.claimPresent(delivery.ID) {
				t.Fatalf("认领未落账")
			}
			if err := tc.mark(store, delivery.ID); err != nil {
				t.Fatalf("Mark%s: %v", tc.name, err)
			}
			if store.claimPresent(delivery.ID) {
				t.Fatalf("%s 后认领未清空", tc.name)
			}
		})
	}
}
