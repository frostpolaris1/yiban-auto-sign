package notify_test

// 0v8 B 投递用例判据：站内信 + 邮件两个渠道的开关、幂等、重试与隐私。
//
// 覆盖：默认订阅下凭据错只进站内信；显式开 email 后恰一条账本行；
// 重复事件不重复投递；mail_notify=false 的属主收不到用户邮件；
// 容量事件同业务键只落一行；重试 1m/5m/30m 后落 failed；
// 不可重试与 panic 立即 failed；SMTP 未配置落 skipped；
// 通知文本、邮件正文与日志都不含完整手机号/邮箱。

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	appnotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/notify"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coremail "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mail"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
)

// manualClock 是可推进的时钟：重试档位按分钟计，测试直接拨表。
type manualClock struct {
	mu  sync.Mutex
	now time.Time
}

func newManualClock() *manualClock {
	return &manualClock{now: time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)}
}

func (c *manualClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *manualClock) Advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.now = c.now.Add(d)
}

// fakeStore 是站内信与账本的内存实现，语义与 pg 一致：
// (user_id, dedupe_key) 与 (notification_id, channel, user_id) 各一道唯一键。
type fakeStore struct {
	mu            sync.Mutex
	notifications map[string]*corenotify.Notification
	order         []string
	deliveries    map[string]*corenotify.DueDelivery
	deliveryOrder []string
	emails        map[string]string
	seq           int
}

func newFakeStore() *fakeStore {
	return &fakeStore{
		notifications: map[string]*corenotify.Notification{},
		deliveries:    map[string]*corenotify.DueDelivery{},
		emails:        map[string]string{},
	}
}

func notificationKey(userID, dedupe string) string { return userID + "\x00" + dedupe }

func deliveryKey(notificationID, userID string) string {
	return notificationID + "\x00" + string(corenotify.ChannelEmail) + "\x00" + userID
}

func (s *fakeStore) Insert(_ context.Context, in corenotify.NotificationInsert) (corenotify.Notification, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.insertLocked(in), nil
}

// insertLocked 写一条通知。调用方已持锁。
func (s *fakeStore) insertLocked(in corenotify.NotificationInsert) corenotify.Notification {
	key := notificationKey(in.UserID, in.DedupeKey)
	if existing, ok := s.notifications[key]; ok {
		return *existing
	}
	s.seq++
	item := corenotify.Notification{
		ID:        fmt.Sprintf("notification-%d", s.seq),
		Category:  in.Category,
		Title:     in.Title,
		Body:      in.Body,
		SchoolID:  in.SchoolID,
		CreatedAt: in.CreatedAt,
	}
	s.notifications[key] = &item
	s.order = append(s.order, key)
	return item
}

func (s *fakeStore) InsertWithPending(_ context.Context, note corenotify.NotificationInsert,
	pending corenotify.DeliveryInsert) (corenotify.NotificationWithDelivery, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	notification := s.insertLocked(note)
	pending.NotificationID = notification.ID
	pending.UserID = note.UserID
	return corenotify.NotificationWithDelivery{
		Notification: notification,
		Delivery:     s.insertPendingLocked(pending),
	}, nil
}

func (s *fakeStore) InsertPending(_ context.Context, in corenotify.DeliveryInsert) (corenotify.Delivery, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.insertPendingLocked(in), nil
}

// insertPendingLocked 写一条账本行。调用方已持锁。
func (s *fakeStore) insertPendingLocked(in corenotify.DeliveryInsert) corenotify.Delivery {
	key := deliveryKey(in.NotificationID, in.UserID)
	if existing, ok := s.deliveries[key]; ok {
		return existing.Delivery
	}
	next := in.NextRetryAt
	item := &corenotify.DueDelivery{
		Delivery: corenotify.Delivery{
			ID:             fmt.Sprintf("delivery-%d", len(s.deliveryOrder)+1),
			NotificationID: in.NotificationID,
			UserID:         in.UserID,
			Channel:        in.Channel,
			Status:         corenotify.DeliveryStatusPending,
			NextRetryAt:    next,
		},
		Email: s.emails[in.UserID],
	}
	s.deliveries[key] = item
	s.deliveryOrder = append(s.deliveryOrder, key)
	return item.Delivery
}

func (s *fakeStore) ListDueForSend(_ context.Context, now time.Time, limit int) ([]corenotify.DueDelivery, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []corenotify.DueDelivery
	for _, key := range s.deliveryOrder {
		if len(out) >= limit {
			break
		}
		item := s.deliveries[key]
		if item.Status != corenotify.DeliveryStatusPending || item.NextRetryAt == nil ||
			item.NextRetryAt.After(now) {
			continue
		}
		row := *item
		row.Title, row.Body = s.copyText(item.NotificationID)
		out = append(out, row)
	}
	return out, nil
}

// copyText 取通知的落库文本。调用方已持锁。
func (s *fakeStore) copyText(notificationID string) (string, string) {
	for _, key := range s.order {
		item := s.notifications[key]
		if item.ID == notificationID {
			return item.Title, item.Body
		}
	}
	return "", ""
}

func (s *fakeStore) MarkSent(_ context.Context, id string) error {
	return s.mutate(id, func(item *corenotify.DueDelivery) {
		item.Status = corenotify.DeliveryStatusSent
		item.Attempts++
		item.SkipReason = ""
		item.NextRetryAt = nil
	})
}

func (s *fakeStore) MarkFailed(_ context.Context, id, kind string, next *time.Time) error {
	return s.mutate(id, func(item *corenotify.DueDelivery) {
		if next == nil {
			item.Status = corenotify.DeliveryStatusFailed
		} else {
			item.Status = corenotify.DeliveryStatusPending
		}
		item.Attempts++
		item.LastErrorKind = kind
		item.SkipReason = ""
		item.NextRetryAt = next
	})
}

func (s *fakeStore) MarkSkipped(_ context.Context, id, reason string) error {
	return s.mutate(id, func(item *corenotify.DueDelivery) {
		if item.Status != corenotify.DeliveryStatusPending {
			return
		}
		item.Status = corenotify.DeliveryStatusSkipped
		item.SkipReason = reason
		item.LastErrorKind = ""
		item.NextRetryAt = nil
	})
}

func (s *fakeStore) mutate(id string, fn func(*corenotify.DueDelivery)) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, key := range s.deliveryOrder {
		if s.deliveries[key].ID == id {
			fn(s.deliveries[key])
			return nil
		}
	}
	return errs.ErrNotFound
}

// notificationList 返回按插入顺序的通知副本。
func (s *fakeStore) notificationList() []corenotify.Notification {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]corenotify.Notification, 0, len(s.order))
	for _, key := range s.order {
		out = append(out, *s.notifications[key])
	}
	return out
}

// deliveryList 返回按插入顺序的账本副本。
func (s *fakeStore) deliveryList() []corenotify.DueDelivery {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]corenotify.DueDelivery, 0, len(s.deliveryOrder))
	for _, key := range s.deliveryOrder {
		out = append(out, *s.deliveries[key])
	}
	return out
}

// fakeAudience 固定返回注入的收件人、手机号与属主。
// inactive 里的 userID 视为已停用/软删；其余一律 active。
type fakeAudience struct {
	subscribers []corenotify.Subscriber
	phones      map[string]string
	owners      map[string]corenotify.Owner
	inactive    map[string]bool
}

func (a *fakeAudience) AdminSubscribers(context.Context, string) ([]corenotify.Subscriber, error) {
	return a.subscribers, nil
}

func (a *fakeAudience) AccountPhone(_ context.Context, accountID string) (string, error) {
	phone, ok := a.phones[accountID]
	if !ok {
		return "", errs.ErrNotFound
	}
	return phone, nil
}

func (a *fakeAudience) FailureOwner(_ context.Context, accountID string) (corenotify.Owner, error) {
	owner, ok := a.owners[accountID]
	if !ok {
		return corenotify.Owner{}, errs.ErrNotFound
	}
	return owner, nil
}

func (a *fakeAudience) RecipientActive(_ context.Context, userID string) (bool, error) {
	return !a.inactive[userID], nil
}

// fakeSender 按脚本返回结果，并记录已发邮件。
type fakeSender struct {
	mu       sync.Mutex
	results  []error
	sent     []coremail.Message
	panicNow bool
}

func (s *fakeSender) Send(_ context.Context, msg coremail.Message) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.panicNow {
		panic("smtp client panic")
	}
	s.sent = append(s.sent, msg)
	if len(s.results) == 0 {
		return nil
	}
	err := s.results[0]
	s.results = s.results[1:]
	return err
}

func (s *fakeSender) messages() []coremail.Message {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]coremail.Message(nil), s.sent...)
}

func (s *fakeSender) calls() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.sent)
}

// overriding 构造一位带显式覆盖的管理员收件人。
func overriding(userID, email string, overrides map[corenotify.Pair]bool) corenotify.Subscriber {
	return corenotify.Subscriber{UserID: userID, Email: email, Overrides: overrides}
}

func newDelivery(t *testing.T, store *fakeStore, audience *fakeAudience,
	mail coremail.Sender, clk *manualClock, logs *bytes.Buffer) *appnotify.Dispatcher {
	t.Helper()
	var logger *slog.Logger
	if logs != nil {
		logger = slog.New(slog.NewTextHandler(logs, nil))
	}
	dispatcher, err := appnotify.NewDispatcher(appnotify.DispatcherDeps{
		Notes: store, Ledger: store, Audience: audience,
		Mail: mail, Clock: clk, Logger: logger,
	})
	if err != nil {
		t.Fatalf("NewDispatcher: %v", err)
	}
	return dispatcher
}

func credentialEvent(accountID string) corenotify.Event {
	return corenotify.Event{
		Category:  corenotify.CategoryCredentialInvalid,
		SchoolID:  "school-1",
		AccountID: accountID,
		DedupeKey: "credential_invalid/" + accountID + "/2026-10-29",
		Day:       "2026-10-29",
	}
}

// 默认订阅（credential_invalid：in_app 开、email 关）只落站内信。
// 显式开 email 后恰一条账本行；重复事件仍各一行。
func TestDispatchDefaultsAndExplicitEmailOverride(t *testing.T) {
	store := newFakeStore()
	clk := newManualClock()
	audience := &fakeAudience{
		subscribers: []corenotify.Subscriber{overriding("master-1", "master-1@example.com", nil)},
		phones:      map[string]string{"acct-1": "13800000001"},
	}
	store.emails["master-1"] = "master-1@example.com"
	dispatcher := newDelivery(t, store, audience, &fakeSender{}, clk, nil)

	if err := dispatcher.Dispatch(context.Background(), credentialEvent("acct-1")); err != nil {
		t.Fatalf("Dispatch: %v", err)
	}
	notes := store.notificationList()
	if len(notes) != 1 {
		t.Fatalf("通知 = %d, want 1", len(notes))
	}
	if notes[0].Title != "账号凭据失效" ||
		!strings.Contains(notes[0].Body, "138****0001") ||
		!strings.Contains(notes[0].Body, "2026-10-29") {
		t.Fatalf("通知文本 = %+v", notes[0])
	}
	if got := len(store.deliveryList()); got != 0 {
		t.Fatalf("默认态不应有邮件账本行，得到 %d", got)
	}

	// 同一收件人显式打开 email：下一次 Dispatch 追加账本行。
	audience.subscribers[0].Overrides = map[corenotify.Pair]bool{
		{Category: corenotify.CategoryCredentialInvalid, Channel: corenotify.ChannelEmail}: true,
	}
	if err := dispatcher.Dispatch(context.Background(), credentialEvent("acct-1")); err != nil {
		t.Fatalf("Dispatch: %v", err)
	}
	deliveries := store.deliveryList()
	if len(deliveries) != 1 {
		t.Fatalf("账本行 = %d, want 1", len(deliveries))
	}
	if deliveries[0].Status != corenotify.DeliveryStatusPending ||
		deliveries[0].Attempts != 0 || deliveries[0].Email != "master-1@example.com" {
		t.Fatalf("账本行 = %+v", deliveries[0])
	}

	// 重复事件不重复投递。
	if err := dispatcher.Dispatch(context.Background(), credentialEvent("acct-1")); err != nil {
		t.Fatalf("Dispatch: %v", err)
	}
	if got := len(store.notificationList()); got != 1 {
		t.Fatalf("重复事件通知 = %d, want 1", got)
	}
	if got := len(store.deliveryList()); got != 1 {
		t.Fatalf("重复事件账本 = %d, want 1", got)
	}
}

// mail_notify=false 的属主收不到用户邮件；true 时每账号每日恰一行；
// 同日重复失败仍一行。
func TestDispatchUserFailureHonorsMailNotify(t *testing.T) {
	store := newFakeStore()
	clk := newManualClock()
	audience := &fakeAudience{
		phones: map[string]string{"acct-1": "13800000001", "acct-2": "13800000002"},
		owners: map[string]corenotify.Owner{
			"acct-1": {UserID: "owner-1", MailNotify: false},
			"acct-2": {UserID: "owner-2", MailNotify: true},
		},
	}
	store.emails["owner-2"] = "owner-2@example.com"
	dispatcher := newDelivery(t, store, audience, &fakeSender{}, clk, nil)

	failure := func(accountID string) corenotify.UserFailure {
		return corenotify.UserFailure{
			SchoolID: "school-1", AccountID: accountID, Day: "2026-10-29",
			Outcome: "credential_invalid", Reason: "登录失败: 账号 13800000001 或密码错误",
		}
	}

	if err := dispatcher.DispatchUserFailure(context.Background(), failure("acct-1")); err != nil {
		t.Fatalf("DispatchUserFailure: %v", err)
	}
	if len(store.notificationList()) != 0 || len(store.deliveryList()) != 0 {
		t.Fatal("mail_notify=false 仍投递")
	}

	if err := dispatcher.DispatchUserFailure(context.Background(), failure("acct-2")); err != nil {
		t.Fatalf("DispatchUserFailure: %v", err)
	}
	if err := dispatcher.DispatchUserFailure(context.Background(), failure("acct-2")); err != nil {
		t.Fatalf("DispatchUserFailure: %v", err)
	}
	notes := store.notificationList()
	if len(notes) != 1 {
		t.Fatalf("用户失败通知 = %d, want 1", len(notes))
	}
	if notes[0].Category != corenotify.CategorySignFailedDaily ||
		notes[0].Title != "账号 138****0002 签到失败" ||
		!strings.Contains(notes[0].Body, "credential_invalid") ||
		!strings.Contains(notes[0].Body, "原因：") {
		t.Fatalf("通知 = %+v", notes[0])
	}
	if mask.TextHasPhone(notes[0].Title) || mask.TextHasPhone(notes[0].Body) {
		t.Fatalf("通知文本残留未掩码手机号：%q / %q", notes[0].Title, notes[0].Body)
	}
	if len(store.deliveryList()) != 1 {
		t.Fatalf("用户失败账本 = %d, want 1", len(store.deliveryList()))
	}
}

// 容量事件同任务同业务日的重复缺口只落一行。
func TestDispatchCapacityRejectedIsIdempotent(t *testing.T) {
	store := newFakeStore()
	clk := newManualClock()
	audience := &fakeAudience{
		subscribers: []corenotify.Subscriber{overriding("master-1", "master-1@example.com", nil)},
	}
	dispatcher := newDelivery(t, store, audience, &fakeSender{}, clk, nil)
	event := corenotify.Event{
		Category:  corenotify.CategoryCapacityRejected,
		SchoolID:  "school-1",
		DedupeKey: "capacity_rejected/task-1/2026-10-29",
		Day:       "2026-10-29",
		Capacity: corenotify.CapacityFigures{
			Demand: 500, Capacity: 120, Shortfall: 380, Binding: "workers", SuggestedWorkers: 8,
		},
	}
	for i := 0; i < 2; i++ {
		if err := dispatcher.Dispatch(context.Background(), event); err != nil {
			t.Fatalf("Dispatch: %v", err)
		}
	}
	notes := store.notificationList()
	if len(notes) != 1 {
		t.Fatalf("容量通知 = %d, want 1", len(notes))
	}
	if notes[0].Title != "容量不足" ||
		!strings.Contains(notes[0].Body, "需求 500") ||
		!strings.Contains(notes[0].Body, "容量 120") ||
		!strings.Contains(notes[0].Body, "缺口 380") ||
		!strings.Contains(notes[0].Body, "瓶颈：workers") ||
		!strings.Contains(notes[0].Body, "建议 YIBAN_WORKERS=8") {
		t.Fatalf("容量文本 = %+v", notes[0])
	}
}

// 可重试失败按 1m/5m/30m 退避，第 4 次失败落 failed。
func TestSendDueRetriesThenFails(t *testing.T) {
	store := newFakeStore()
	clk := newManualClock()
	audience := &fakeAudience{
		subscribers: []corenotify.Subscriber{
			overriding("master-1", "master-1@example.com", map[corenotify.Pair]bool{
				{Category: corenotify.CategoryReviewPending, Channel: corenotify.ChannelEmail}: true,
			}),
		},
		phones: map[string]string{"acct-1": "13800000001"},
	}
	store.emails["master-1"] = "master-1@example.com"
	sender := &fakeSender{results: []error{
		&coremail.DeliveryError{Kind: coremail.KindConnect, Retryable: true},
		&coremail.DeliveryError{Kind: coremail.KindTimeout, Retryable: true},
		&coremail.DeliveryError{Kind: coremail.KindConnect, Retryable: true},
		&coremail.DeliveryError{Kind: coremail.KindConnect, Retryable: true},
	}}
	dispatcher := newDelivery(t, store, audience, sender, clk, nil)
	event := corenotify.Event{
		Category: corenotify.CategoryReviewPending, SchoolID: "school-1",
		AccountID: "acct-1", DedupeKey: "review_pending/acct-1/evt-1",
	}
	if err := dispatcher.Dispatch(context.Background(), event); err != nil {
		t.Fatalf("Dispatch: %v", err)
	}

	wantDelays := []time.Duration{time.Minute, 5 * time.Minute, 30 * time.Minute}
	for attempt, delay := range wantDelays {
		if err := dispatcher.SendDue(context.Background()); err != nil {
			t.Fatalf("SendDue: %v", err)
		}
		deliveries := store.deliveryList()
		if len(deliveries) != 1 {
			t.Fatalf("账本行 = %d", len(deliveries))
		}
		got := deliveries[0]
		if got.Status != corenotify.DeliveryStatusPending || got.Attempts != attempt+1 {
			t.Fatalf("第 %d 次失败后 = %s/%d", attempt+1, got.Status, got.Attempts)
		}
		if got.NextRetryAt == nil || !got.NextRetryAt.Equal(clk.Now().Add(delay)) {
			t.Fatalf("第 %d 次 next_retry_at = %v, want %v", attempt+1, got.NextRetryAt, clk.Now().Add(delay))
		}
		if got.LastErrorKind == "" {
			t.Fatalf("第 %d 次缺 last_error_kind", attempt+1)
		}
		clk.Advance(delay)
	}
	// 第 4 次失败：重试次数用完，落 failed。
	if err := dispatcher.SendDue(context.Background()); err != nil {
		t.Fatalf("SendDue: %v", err)
	}
	deliveries := store.deliveryList()
	if deliveries[0].Status != corenotify.DeliveryStatusFailed || deliveries[0].Attempts != 4 ||
		deliveries[0].NextRetryAt != nil {
		t.Fatalf("终态 = %+v", deliveries[0])
	}
	if sender.calls() != 4 {
		t.Fatalf("投递次数 = %d, want 4", sender.calls())
	}
	// 已 failed 的行不再被扫描。
	clk.Advance(24 * time.Hour)
	if err := dispatcher.SendDue(context.Background()); err != nil {
		t.Fatalf("SendDue: %v", err)
	}
	if sender.calls() != 4 {
		t.Fatalf("failed 行仍被重投，次数 = %d", sender.calls())
	}
}

// 不可重试错误与 panic 立即 failed，不排下一次。
func TestSendDueTerminalFailures(t *testing.T) {
	for _, tc := range []struct {
		name     string
		sender   *fakeSender
		wantKind coremail.Kind
	}{
		{"auth", &fakeSender{results: []error{
			&coremail.DeliveryError{Kind: coremail.KindAuth, Retryable: false},
		}}, coremail.KindAuth},
		{"panic", &fakeSender{panicNow: true}, coremail.KindPanic},
	} {
		t.Run(tc.name, func(t *testing.T) {
			store := newFakeStore()
			clk := newManualClock()
			audience := &fakeAudience{
				subscribers: []corenotify.Subscriber{
					overriding("master-1", "master-1@example.com", map[corenotify.Pair]bool{
						{Category: corenotify.CategoryReviewPending, Channel: corenotify.ChannelEmail}: true,
					}),
				},
				phones: map[string]string{"acct-1": "13800000001"},
			}
			dispatcher := newDelivery(t, store, audience, tc.sender, clk, nil)
			if err := dispatcher.Dispatch(context.Background(), corenotify.Event{
				Category: corenotify.CategoryReviewPending, SchoolID: "school-1",
				AccountID: "acct-1", DedupeKey: "review_pending/acct-1/evt-1",
			}); err != nil {
				t.Fatalf("Dispatch: %v", err)
			}
			if err := dispatcher.SendDue(context.Background()); err != nil {
				t.Fatalf("SendDue: %v", err)
			}
			got := store.deliveryList()[0]
			if got.Status != corenotify.DeliveryStatusFailed || got.Attempts != 1 ||
				got.LastErrorKind != string(tc.wantKind) || got.NextRetryAt != nil {
				t.Fatalf("终态 = %+v, want failed/1/%s", got, tc.wantKind)
			}
		})
	}
}

// SMTP 未配置：仍写通知与账本行，账本立即 skipped，并记一条显性警告。
func TestDispatchWithoutSMTPMarksSkipped(t *testing.T) {
	store := newFakeStore()
	clk := newManualClock()
	audience := &fakeAudience{
		subscribers: []corenotify.Subscriber{
			overriding("master-1", "master-1@example.com", map[corenotify.Pair]bool{
				{Category: corenotify.CategoryReviewPending, Channel: corenotify.ChannelEmail}: true,
			}),
		},
		phones: map[string]string{"acct-1": "13800000001"},
	}
	var logs bytes.Buffer
	dispatcher := newDelivery(t, store, audience, nil, clk, &logs)
	if err := dispatcher.Dispatch(context.Background(), corenotify.Event{
		Category: corenotify.CategoryReviewPending, SchoolID: "school-1",
		AccountID: "acct-1", DedupeKey: "review_pending/acct-1/evt-1",
	}); err != nil {
		t.Fatalf("Dispatch: %v", err)
	}
	if len(store.notificationList()) != 1 {
		t.Fatalf("通知 = %d, want 1", len(store.notificationList()))
	}
	deliveries := store.deliveryList()
	if len(deliveries) != 1 || deliveries[0].Status != corenotify.DeliveryStatusSkipped ||
		deliveries[0].SkipReason != "smtp_not_configured" || deliveries[0].Attempts != 0 {
		t.Fatalf("账本 = %+v", deliveries)
	}
	if !strings.Contains(logs.String(), "smtp_not_configured") {
		t.Fatalf("缺显性警告：%s", logs.String())
	}
}

// 投递前复核收件人：入队后软删/停用的用户不发信，账本落 skipped。
// 跳过原因与用户标识进日志，邮箱不进日志。
func TestSendDueSkipsInactiveRecipient(t *testing.T) {
	store := newFakeStore()
	clk := newManualClock()
	audience := &fakeAudience{
		subscribers: []corenotify.Subscriber{
			overriding("master-1", "master-1@example.com", map[corenotify.Pair]bool{
				{Category: corenotify.CategoryCredentialInvalid, Channel: corenotify.ChannelEmail}: true,
			}),
		},
		phones:   map[string]string{"acct-1": "13800000001"},
		inactive: map[string]bool{"master-1": true},
	}
	store.emails["master-1"] = "master-1@example.com"
	sender := &fakeSender{}
	var logs bytes.Buffer
	dispatcher := newDelivery(t, store, audience, sender, clk, &logs)

	if err := dispatcher.Dispatch(context.Background(), credentialEvent("acct-1")); err != nil {
		t.Fatalf("Dispatch: %v", err)
	}
	// 入队时仍活跃（投递前复核尚未发生）：通知与账本各一行。
	if len(store.notificationList()) != 1 || len(store.deliveryList()) != 1 {
		t.Fatalf("入队行数 = %d/%d", len(store.notificationList()), len(store.deliveryList()))
	}
	if err := dispatcher.SendDue(context.Background()); err != nil {
		t.Fatalf("SendDue: %v", err)
	}
	if sender.calls() != 0 {
		t.Fatalf("不活跃收件人仍被投递 %d 次", sender.calls())
	}
	delivery := store.deliveryList()[0]
	if delivery.Status != corenotify.DeliveryStatusSkipped ||
		delivery.SkipReason != "recipient_inactive" || delivery.Attempts != 0 ||
		delivery.NextRetryAt != nil {
		t.Fatalf("账本 = %+v", delivery)
	}
	logged := logs.String()
	if !strings.Contains(logged, "recipient_inactive") || !strings.Contains(logged, "master-1") {
		t.Fatalf("缺跳过原因或用户标识：%s", logged)
	}
	if strings.Contains(logged, "master-1@example.com") {
		t.Fatalf("日志含完整邮箱：%s", logged)
	}
}

// in_app 关闭、email 开启：锚点行仍会写（会出现在管理员站内信列表）。
// 这是实施单裁决：账本外键需要父行，本批不为可见性拆表。
func TestEmailOnlyStillWritesAnchorNotification(t *testing.T) {
	store := newFakeStore()
	clk := newManualClock()
	audience := &fakeAudience{
		subscribers: []corenotify.Subscriber{
			overriding("master-1", "master-1@example.com", map[corenotify.Pair]bool{
				{Category: corenotify.CategoryCredentialInvalid, Channel: corenotify.ChannelInApp}: false,
				{Category: corenotify.CategoryCredentialInvalid, Channel: corenotify.ChannelEmail}: true,
			}),
		},
		phones: map[string]string{"acct-1": "13800000001"},
	}
	store.emails["master-1"] = "master-1@example.com"
	dispatcher := newDelivery(t, store, audience, &fakeSender{}, clk, nil)

	if err := dispatcher.Dispatch(context.Background(), credentialEvent("acct-1")); err != nil {
		t.Fatalf("Dispatch: %v", err)
	}
	notes := store.notificationList()
	if len(notes) != 1 {
		t.Fatalf("锚点行 = %d, want 1（in_app 关闭不影响账本锚）", len(notes))
	}
	if notes[0].Title != "账号凭据失效" {
		t.Fatalf("锚点文本 = %+v", notes[0])
	}
	deliveries := store.deliveryList()
	if len(deliveries) != 1 || deliveries[0].Status != corenotify.DeliveryStatusPending {
		t.Fatalf("账本 = %+v", deliveries)
	}
	// 两个开关都关才整条跳过。
	audience.subscribers[0].Overrides = map[corenotify.Pair]bool{
		{Category: corenotify.CategoryCredentialInvalid, Channel: corenotify.ChannelInApp}: false,
		{Category: corenotify.CategoryCredentialInvalid, Channel: corenotify.ChannelEmail}: false,
	}
	other := credentialEvent("acct-1")
	other.DedupeKey = "credential_invalid/acct-1/other"
	if err := dispatcher.Dispatch(context.Background(), other); err != nil {
		t.Fatalf("Dispatch: %v", err)
	}
	if len(store.notificationList()) != 1 || len(store.deliveryList()) != 1 {
		t.Fatalf("两开关都关仍写行：%d/%d", len(store.notificationList()), len(store.deliveryList()))
	}
}

// 隐私：通知文本、邮件正文与日志都不含完整手机号/邮箱。
func TestDeliveryTextsAndLogsCarryNoFullIdentifiers(t *testing.T) {
	const phone = "13800000001"
	const email = "master-1@example.com"
	store := newFakeStore()
	clk := newManualClock()
	audience := &fakeAudience{
		subscribers: []corenotify.Subscriber{
			overriding("master-1", email, map[corenotify.Pair]bool{
				{Category: corenotify.CategoryCredentialInvalid, Channel: corenotify.ChannelEmail}: true,
			}),
		},
		phones: map[string]string{"acct-1": phone},
	}
	store.emails["master-1"] = email
	sender := &fakeSender{}
	var logs bytes.Buffer
	dispatcher := newDelivery(t, store, audience, sender, clk, &logs)

	if err := dispatcher.Dispatch(context.Background(), credentialEvent("acct-1")); err != nil {
		t.Fatalf("Dispatch: %v", err)
	}
	if err := dispatcher.SendDue(context.Background()); err != nil {
		t.Fatalf("SendDue: %v", err)
	}
	note := store.notificationList()[0]
	if strings.Contains(note.Title, phone) || strings.Contains(note.Body, phone) ||
		mask.TextHasPhone(note.Title) || mask.TextHasPhone(note.Body) {
		t.Fatalf("通知文本含完整手机号：%+v", note)
	}
	messages := sender.messages()
	if len(messages) != 1 {
		t.Fatalf("邮件 = %d, want 1", len(messages))
	}
	msg := messages[0]
	if msg.To != email {
		t.Fatalf("收件地址 = %q", msg.To)
	}
	if !strings.HasPrefix(msg.Subject, "【易班签到】") ||
		!strings.Contains(msg.Text, "此邮件由系统自动发送，请勿回复。") {
		t.Fatalf("邮件模板 = %q / %q", msg.Subject, msg.Text)
	}
	if strings.Contains(msg.Subject, phone) || strings.Contains(msg.Text, phone) ||
		mask.TextHasPhone(msg.Subject) || mask.TextHasPhone(msg.Text) {
		t.Fatalf("邮件文本含完整手机号：%q / %q", msg.Subject, msg.Text)
	}
	logged := logs.String()
	if strings.Contains(logged, phone) {
		t.Fatalf("日志含完整手机号：%s", logged)
	}
	if strings.Contains(logged, email) {
		t.Fatalf("日志含完整邮箱：%s", logged)
	}
}

// Start 启动即跑一轮，Stop 可重复调用且不留循环。
func TestStartDeliversPendingAndStops(t *testing.T) {
	store := newFakeStore()
	clk := newManualClock()
	audience := &fakeAudience{
		subscribers: []corenotify.Subscriber{
			overriding("master-1", "master-1@example.com", map[corenotify.Pair]bool{
				{Category: corenotify.CategoryReviewPending, Channel: corenotify.ChannelEmail}: true,
			}),
		},
		phones: map[string]string{"acct-1": "13800000001"},
	}
	store.emails["master-1"] = "master-1@example.com"
	sender := &fakeSender{}
	dispatcher := newDelivery(t, store, audience, sender, clk, nil)

	if err := dispatcher.Dispatch(context.Background(), corenotify.Event{
		Category: corenotify.CategoryReviewPending, SchoolID: "school-1",
		AccountID: "acct-1", DedupeKey: "review_pending/acct-1/evt-1",
	}); err != nil {
		t.Fatalf("Dispatch: %v", err)
	}
	stop := appnotify.Start(context.Background(), dispatcher, nil)
	// 启动轮在 goroutine 里跑：用有界 select 等结果，不读系统时钟。
	deadline := time.After(5 * time.Second)
	for sender.calls() == 0 {
		select {
		case <-deadline:
			stop()
			t.Fatalf("启动轮未投递，次数 = %d", sender.calls())
		case <-time.After(10 * time.Millisecond):
		}
	}
	stop()
	stop() // 幂等

	if got := store.deliveryList()[0].Status; got != corenotify.DeliveryStatusSent {
		t.Fatalf("终态 = %s, want sent", got)
	}
}

// 未支持类别与缺业务键在用例层拒绝。
func TestDispatchValidatesInput(t *testing.T) {
	store := newFakeStore()
	clk := newManualClock()
	audience := &fakeAudience{phones: map[string]string{"acct-1": "13800000001"}}
	dispatcher := newDelivery(t, store, audience, &fakeSender{}, clk, nil)

	// sign_failed_daily 的管理员摘要是 C 批 DispatchDigest 的入口：
	// Dispatch 明确拒绝，避免两条投递路径共用同一收件人判定。
	if err := dispatcher.Dispatch(context.Background(), corenotify.Event{
		Category: corenotify.CategorySignFailedDaily, SchoolID: "school-1",
		AccountID: "acct-1", DedupeKey: "x",
	}); errs.From(err).Code != errs.CodeValidation {
		t.Fatalf("sign_failed_daily 经 Dispatch 的错误 = %v", errs.From(err).Code)
	}
	if err := dispatcher.Dispatch(context.Background(), corenotify.Event{
		Category: corenotify.CategoryReviewPending, AccountID: "acct-1",
	}); errs.From(err).Code != errs.CodeValidation {
		t.Fatalf("缺 dedupe_key 的错误 = %v", errs.From(err).Code)
	}
	if _, err := appnotify.NewDispatcher(appnotify.DispatcherDeps{
		Ledger: store, Audience: audience, Clock: clk,
	}); err == nil {
		t.Fatal("缺站内信写入端口应构造失败")
	}
}

// 未知收件人的用户失败邮件是空操作，不是错误。
func TestDispatchUserFailureSkipsMissingOwner(t *testing.T) {
	store := newFakeStore()
	clk := newManualClock()
	audience := &fakeAudience{phones: map[string]string{"acct-1": "13800000001"}}
	dispatcher := newDelivery(t, store, audience, &fakeSender{}, clk, nil)

	if err := dispatcher.DispatchUserFailure(context.Background(), corenotify.UserFailure{
		AccountID: "acct-1", Day: "2026-10-29", Outcome: "blocked",
	}); err != nil {
		t.Fatalf("DispatchUserFailure: %v", err)
	}
	if len(store.notificationList()) != 0 || len(store.deliveryList()) != 0 {
		t.Fatal("无属主仍写行")
	}
	// 缺账号或业务日才是调用方缺陷。
	if err := dispatcher.DispatchUserFailure(context.Background(), corenotify.UserFailure{
		Day: "2026-10-29",
	}); errs.From(err).Code != errs.CodeValidation {
		t.Fatalf("缺账号的错误 = %v", errs.From(err).Code)
	}
}

// errors.Is 链（coremail.Classify 之外的错误）按不可重试处理。
func TestSendDueUnknownErrorIsTerminal(t *testing.T) {
	store := newFakeStore()
	clk := newManualClock()
	audience := &fakeAudience{
		subscribers: []corenotify.Subscriber{
			overriding("master-1", "master-1@example.com", map[corenotify.Pair]bool{
				{Category: corenotify.CategoryReviewPending, Channel: corenotify.ChannelEmail}: true,
			}),
		},
		phones: map[string]string{"acct-1": "13800000001"},
	}
	sender := &fakeSender{results: []error{errors.New("dial tcp: connection refused")}}
	dispatcher := newDelivery(t, store, audience, sender, clk, nil)
	if err := dispatcher.Dispatch(context.Background(), corenotify.Event{
		Category: corenotify.CategoryReviewPending, SchoolID: "school-1",
		AccountID: "acct-1", DedupeKey: "review_pending/acct-1/evt-1",
	}); err != nil {
		t.Fatalf("Dispatch: %v", err)
	}
	if err := dispatcher.SendDue(context.Background()); err != nil {
		t.Fatalf("SendDue: %v", err)
	}
	got := store.deliveryList()[0]
	if got.Status != corenotify.DeliveryStatusFailed || got.LastErrorKind != string(coremail.KindProtocol) {
		t.Fatalf("终态 = %+v, want failed/protocol", got)
	}
}
