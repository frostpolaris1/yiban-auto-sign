package notify_test

// 0v8 C 每日签到失败摘要的用例判据。
//
// 覆盖：空日不发；master 全量 / admin 绑定校 / 无绑定 admin 不发；
// 同账号多任务聚合（最新 result、最大 attempts）；封顶 50 行 + 另有 N 条；
// 同收件人同日重复触发一行通知、一条账本、一封邮件；
// 两开关不 OR（prefs 关闭即不发，缺行按默认 email=true）；
// 正文与日志不含完整手机号/邮箱；摘要正文上限 4000 字符。

import (
	"bytes"
	"context"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"
	"unicode/utf8"

	appnotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/notify"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coremail "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mail"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
)

const digestDay = "2026-10-29"

// fakeDigestSource 按业务日返回注入的失败明细，并记录查询过的业务日。
// panicNow 用于断言宿主 recover：panic 在记录查询之后发生。
type fakeDigestSource struct {
	mu       sync.Mutex
	tasks    map[string][]corenotify.FailedTask
	days     []string
	err      error
	panicNow bool
	// called 非 nil 时每次查询投递业务日；用于等待后台补发。
	called chan string
}

func newFakeDigestSource(tasks ...corenotify.FailedTask) *fakeDigestSource {
	source := &fakeDigestSource{tasks: map[string][]corenotify.FailedTask{}}
	if len(tasks) > 0 {
		source.tasks[digestDay] = tasks
	}
	return source
}

func (s *fakeDigestSource) FailedAccounts(_ context.Context, day string) ([]corenotify.FailedTask, error) {
	s.mu.Lock()
	s.days = append(s.days, day)
	tasks := append([]corenotify.FailedTask(nil), s.tasks[day]...)
	err := s.err
	panicNow := s.panicNow
	called := s.called
	s.mu.Unlock()
	if called != nil {
		select {
		case called <- day:
		default:
		}
	}
	if panicNow {
		panic("digest source panic")
	}
	return tasks, err
}

func (s *fakeDigestSource) queriedDays() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string(nil), s.days...)
}

// fakeDigestAudience 固定返回注入的摘要收件人，并记录调用次数。
// 空日不发的判据之一是根本不解析收件人。
type fakeDigestAudience struct {
	mu          sync.Mutex
	subscribers []corenotify.DigestSubscriber
	err         error
	calls       int
}

func (a *fakeDigestAudience) DigestSubscribers(context.Context) ([]corenotify.DigestSubscriber, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.calls++
	return a.subscribers, a.err
}

func (a *fakeDigestAudience) callCount() int {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.calls
}

// lookupNotification 按 (user, dedupe_key) 取通知行。
func (s *fakeStore) lookupNotification(userID, dedupe string) (corenotify.Notification, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	item, ok := s.notifications[notificationKey(userID, dedupe)]
	if !ok {
		return corenotify.Notification{}, false
	}
	return *item, true
}

// digestSub 构造一位摘要收件人。
func digestSub(userID, email string, master bool, schools []string,
	overrides map[corenotify.Pair]bool) corenotify.DigestSubscriber {
	return corenotify.DigestSubscriber{
		UserID: userID, Email: email, Master: master,
		Schools: schools, Overrides: overrides,
	}
}

// failedTask 构造一条失败明细。
func failedTask(accountID, schoolID, phone, result string, attempts int, at time.Time) corenotify.FailedTask {
	return corenotify.FailedTask{
		AccountID: accountID, SchoolID: schoolID, Phone: phone,
		Result: result, Attempts: attempts, UpdatedAt: at,
	}
}

// newDigest 用内存仓储与摘要端口构造投递服务。
func newDigest(t *testing.T, store *fakeStore, source appnotify.DigestSource,
	audience appnotify.DigestAudience, mail coremail.Sender,
	clk *manualClock, logs *bytes.Buffer) *appnotify.Dispatcher {
	t.Helper()
	var logger *slog.Logger
	if logs != nil {
		logger = slog.New(slog.NewTextHandler(logs, nil))
	}
	dispatcher, err := appnotify.NewDispatcher(appnotify.DispatcherDeps{
		Notes: store, Ledger: store, Audience: &fakeAudience{},
		DigestSource: source, DigestAudience: audience,
		Mail: mail, Clock: clk, Logger: logger,
	})
	if err != nil {
		t.Fatalf("NewDispatcher: %v", err)
	}
	return dispatcher
}

// 昨日 failed=0：不写通知、不写账本、不发信。
func TestDispatchDigestEmptyDaySendsNothing(t *testing.T) {
	store := newFakeStore()
	source := newFakeDigestSource()
	audience := &fakeDigestAudience{subscribers: []corenotify.DigestSubscriber{
		digestSub("master-1", "master-1@example.com", true, nil, nil),
	}}
	sender := &fakeSender{}
	dispatcher := newDigest(t, store, source, audience, sender, newManualClock(), nil)

	if err := dispatcher.DispatchDigest(context.Background(), digestDay); err != nil {
		t.Fatalf("DispatchDigest: %v", err)
	}
	if got := len(store.notificationList()); got != 0 {
		t.Fatalf("空日通知 = %d, want 0", got)
	}
	if got := len(store.deliveryList()); got != 0 {
		t.Fatalf("空日账本 = %d, want 0", got)
	}
	if sender.calls() != 0 {
		t.Fatalf("空日发信 %d 次", sender.calls())
	}
	if days := source.queriedDays(); len(days) != 1 || days[0] != digestDay {
		t.Fatalf("查询业务日 = %v, want [%s]", days, digestDay)
	}
	if got := audience.callCount(); got != 0 {
		t.Fatalf("空日解析收件人 %d 次, want 0", got)
	}
}

// master 收全量；admin 只收绑定校；无绑定 admin 不发。
func TestDispatchDigestScopesRecipients(t *testing.T) {
	store := newFakeStore()
	source := newFakeDigestSource(
		failedTask("acct-a", "school-a", "13800000001", "blocked", 1, time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)),
		failedTask("acct-b", "school-b", "13800000002", "window_closed", 2, time.Date(2026, 10, 29, 6, 1, 0, 0, time.UTC)),
	)
	audience := &fakeDigestAudience{subscribers: []corenotify.DigestSubscriber{
		digestSub("master-1", "master-1@example.com", true, nil, nil),
		digestSub("admin-a", "admin-a@example.com", false, []string{"school-a"}, nil),
		digestSub("admin-b", "admin-b@example.com", false, []string{"school-b"}, nil),
		digestSub("admin-empty", "empty@example.com", false, []string{"school-empty"}, nil),
		digestSub("admin-unbound", "unbound@example.com", false, nil, nil),
	}}
	dispatcher := newDigest(t, store, source, audience, &fakeSender{}, newManualClock(), nil)

	if err := dispatcher.DispatchDigest(context.Background(), digestDay); err != nil {
		t.Fatalf("DispatchDigest: %v", err)
	}
	if got := len(store.notificationList()); got != 3 {
		t.Fatalf("通知 = %d, want 3（master + 两个有校 admin）", got)
	}
	dedupe := "sign_failed_daily/" + digestDay
	for _, tc := range []struct {
		userID string
		want   []string
		absent []string
	}{
		{"master-1", []string{"138****0001", "138****0002"}, nil},
		{"admin-a", []string{"138****0001"}, []string{"138****0002"}},
		{"admin-b", []string{"138****0002"}, []string{"138****0001"}},
	} {
		note, ok := store.lookupNotification(tc.userID, dedupe)
		if !ok {
			t.Fatalf("收件人 %s 无通知行", tc.userID)
		}
		if note.Category != corenotify.CategorySignFailedDaily ||
			note.Title != digestDay+" 签到失败摘要" {
			t.Fatalf("%s 通知 = %+v", tc.userID, note)
		}
		for _, want := range tc.want {
			if !strings.Contains(note.Body, want) {
				t.Fatalf("%s 正文缺 %s：%q", tc.userID, want, note.Body)
			}
		}
		for _, absent := range tc.absent {
			if strings.Contains(note.Body, absent) {
				t.Fatalf("%s 正文越界含 %s：%q", tc.userID, absent, note.Body)
			}
		}
	}
	if _, ok := store.lookupNotification("admin-unbound", dedupe); ok {
		t.Fatal("无绑定校 admin 收到摘要")
	}
	// scope 内 0 条：有绑定校但没有失败行，该收件人无行。
	if _, ok := store.lookupNotification("admin-empty", dedupe); ok {
		t.Fatal("scope 为空的 admin 收到摘要")
	}
	// master 正文的会计数覆盖全量。
	masterNote, _ := store.lookupNotification("master-1", dedupe)
	if !strings.Contains(masterNote.Body, digestDay+" 共有 2 个账号签到失败。") {
		t.Fatalf("master 首行 = %q", masterNote.Body)
	}
	adminNote, _ := store.lookupNotification("admin-a", dedupe)
	if !strings.Contains(adminNote.Body, digestDay+" 共有 1 个账号签到失败。") {
		t.Fatalf("admin 首行 = %q", adminNote.Body)
	}
}

// 同账号多任务聚合一行：最新 result、最大 attempts；空 result 显示 unknown。
func TestDispatchDigestAggregatesAccounts(t *testing.T) {
	early := time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)
	late := early.Add(5 * time.Minute)
	store := newFakeStore()
	source := newFakeDigestSource(
		failedTask("acct-1", "school-a", "13800000001", "blocked", 1, early),
		failedTask("acct-1", "school-a", "13800000001", "window_closed", 3, late),
		failedTask("acct-2", "school-a", "13800000002", "", 2, early),
	)
	audience := &fakeDigestAudience{subscribers: []corenotify.DigestSubscriber{
		digestSub("master-1", "master-1@example.com", true, nil, nil),
	}}
	dispatcher := newDigest(t, store, source, audience, &fakeSender{}, newManualClock(), nil)

	if err := dispatcher.DispatchDigest(context.Background(), digestDay); err != nil {
		t.Fatalf("DispatchDigest: %v", err)
	}
	note, ok := store.lookupNotification("master-1", "sign_failed_daily/"+digestDay)
	if !ok {
		t.Fatal("缺摘要通知")
	}
	want := digestDay + " 共有 2 个账号签到失败。\n" +
		"138****0001：window_closed（尝试 3 次）\n" +
		"138****0002：unknown（尝试 2 次）"
	if note.Body != want {
		t.Fatalf("正文 = %q\nwant %q", note.Body, want)
	}
	if strings.Count(note.Body, "138****0001") != 1 {
		t.Fatalf("同账号出现多行：%q", note.Body)
	}
}

// 超过 50 个账号：正文恰好 50 行 +「另有 N 条未列出」，且不超过 4000 字符。
func TestDispatchDigestCapsAtFiftyLines(t *testing.T) {
	tasks := make([]corenotify.FailedTask, 0, 60)
	for i := 0; i < 60; i++ {
		tasks = append(tasks, failedTask(
			fmt.Sprintf("acct-%03d", i), "school-a", fmt.Sprintf("138%08d", i),
			"blocked", 1, time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)))
	}
	store := newFakeStore()
	source := newFakeDigestSource(tasks...)
	audience := &fakeDigestAudience{subscribers: []corenotify.DigestSubscriber{
		digestSub("master-1", "master-1@example.com", true, nil, nil),
	}}
	dispatcher := newDigest(t, store, source, audience, &fakeSender{}, newManualClock(), nil)

	if err := dispatcher.DispatchDigest(context.Background(), digestDay); err != nil {
		t.Fatalf("DispatchDigest: %v", err)
	}
	note, ok := store.lookupNotification("master-1", "sign_failed_daily/"+digestDay)
	if !ok {
		t.Fatal("缺摘要通知")
	}
	if !strings.Contains(note.Body, "另有 10 条未列出。") {
		t.Fatalf("缺封顶尾注：%q", note.Body)
	}
	if got := strings.Count(note.Body, "：blocked（尝试 1 次）"); got != 50 {
		t.Fatalf("账号行 = %d, want 50", got)
	}
	if utf8.RuneCountInString(note.Body) > 4000 {
		t.Fatalf("正文 %d 字符，超过 4000", utf8.RuneCountInString(note.Body))
	}
	if utf8.RuneCountInString(note.Title) > 80 {
		t.Fatalf("标题 %d 字符，超过 80", utf8.RuneCountInString(note.Title))
	}
}

// 同收件人同日重复触发：一行通知、一条账本、一封邮件。
func TestDispatchDigestIsIdempotentPerRecipient(t *testing.T) {
	store := newFakeStore()
	source := newFakeDigestSource(
		failedTask("acct-1", "school-a", "13800000001", "blocked", 1, time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)),
	)
	audience := &fakeDigestAudience{subscribers: []corenotify.DigestSubscriber{
		digestSub("master-1", "master-1@example.com", true, nil, nil),
	}}
	store.emails["master-1"] = "master-1@example.com"
	sender := &fakeSender{}
	dispatcher := newDigest(t, store, source, audience, sender, newManualClock(), nil)

	for i := 0; i < 2; i++ {
		if err := dispatcher.DispatchDigest(context.Background(), digestDay); err != nil {
			t.Fatalf("DispatchDigest: %v", err)
		}
	}
	if got := len(store.notificationList()); got != 1 {
		t.Fatalf("重复触发通知 = %d, want 1", got)
	}
	if got := len(store.deliveryList()); got != 1 {
		t.Fatalf("重复触发账本 = %d, want 1", got)
	}
	if err := dispatcher.SendDue(context.Background()); err != nil {
		t.Fatalf("SendDue: %v", err)
	}
	if sender.calls() != 1 {
		t.Fatalf("重复触发邮件 = %d, want 1", sender.calls())
	}
}

// 两个开关分别判定：email=false 且 in_app=false 不发；
// email=false + in_app=true 只写通知行；缺行按默认 email=true。
func TestDispatchDigestRespectsChannelSwitches(t *testing.T) {
	off := map[corenotify.Pair]bool{
		{Category: corenotify.CategorySignFailedDaily, Channel: corenotify.ChannelEmail}: false,
		{Category: corenotify.CategorySignFailedDaily, Channel: corenotify.ChannelInApp}: false,
	}
	inAppOnly := map[corenotify.Pair]bool{
		{Category: corenotify.CategorySignFailedDaily, Channel: corenotify.ChannelEmail}: false,
		{Category: corenotify.CategorySignFailedDaily, Channel: corenotify.ChannelInApp}: true,
	}
	store := newFakeStore()
	source := newFakeDigestSource(
		failedTask("acct-1", "school-a", "13800000001", "blocked", 1, time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)),
	)
	audience := &fakeDigestAudience{subscribers: []corenotify.DigestSubscriber{
		digestSub("both-off", "off@example.com", true, nil, off),
		digestSub("inapp-only", "inapp@example.com", true, nil, inAppOnly),
		digestSub("default", "default@example.com", true, nil, nil),
	}}
	dispatcher := newDigest(t, store, source, audience, &fakeSender{}, newManualClock(), nil)

	if err := dispatcher.DispatchDigest(context.Background(), digestDay); err != nil {
		t.Fatalf("DispatchDigest: %v", err)
	}
	dedupe := "sign_failed_daily/" + digestDay
	if _, ok := store.lookupNotification("both-off", dedupe); ok {
		t.Fatal("两开关都关仍写通知")
	}
	if _, ok := store.lookupNotification("inapp-only", dedupe); !ok {
		t.Fatal("in_app 开、email 关应写通知行")
	}
	if _, ok := store.lookupNotification("default", dedupe); !ok {
		t.Fatal("缺行默认 email=true 应写通知行")
	}
	deliveries := store.deliveryList()
	wantUsers := map[string]bool{"default": true}
	if len(deliveries) != 1 {
		t.Fatalf("账本 = %d, want 1", len(deliveries))
	}
	if !wantUsers[deliveries[0].UserID] {
		t.Fatalf("账本收件人 = %s, want default", deliveries[0].UserID)
	}
}

// SMTP 未配置：仍写通知与账本行，账本立即 skipped，并记显性警告。
func TestDispatchDigestWithoutSMTPMarksSkipped(t *testing.T) {
	store := newFakeStore()
	source := newFakeDigestSource(
		failedTask("acct-1", "school-a", "13800000001", "blocked", 1, time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)),
	)
	audience := &fakeDigestAudience{subscribers: []corenotify.DigestSubscriber{
		digestSub("master-1", "master-1@example.com", true, nil, nil),
	}}
	var logs bytes.Buffer
	dispatcher := newDigest(t, store, source, audience, nil, newManualClock(), &logs)

	if err := dispatcher.DispatchDigest(context.Background(), digestDay); err != nil {
		t.Fatalf("DispatchDigest: %v", err)
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

// 标题/正文/邮件/日志不含完整手机号或邮箱。收件地址只进 SMTP 信封。
func TestDispatchDigestCarriesNoFullIdentifiers(t *testing.T) {
	const phone = "13800000001"
	const email = "master-secret@example.com"
	store := newFakeStore()
	source := newFakeDigestSource(
		failedTask("acct-1", "school-a", phone, "blocked", 1, time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)),
	)
	audience := &fakeDigestAudience{subscribers: []corenotify.DigestSubscriber{
		digestSub("master-1", email, true, nil, nil),
	}}
	store.emails["master-1"] = email
	sender := &fakeSender{}
	var logs bytes.Buffer
	dispatcher := newDigest(t, store, source, audience, sender, newManualClock(), &logs)

	if err := dispatcher.DispatchDigest(context.Background(), digestDay); err != nil {
		t.Fatalf("DispatchDigest: %v", err)
	}
	if err := dispatcher.SendDue(context.Background()); err != nil {
		t.Fatalf("SendDue: %v", err)
	}
	note, _ := store.lookupNotification("master-1", "sign_failed_daily/"+digestDay)
	if strings.Contains(note.Title, phone) || strings.Contains(note.Body, phone) ||
		mask.TextHasPhone(note.Title) || mask.TextHasPhone(note.Body) {
		t.Fatalf("通知文本含完整手机号：%+v", note)
	}
	messages := sender.messages()
	if len(messages) != 1 {
		t.Fatalf("邮件 = %d, want 1", len(messages))
	}
	if messages[0].To != email {
		t.Fatalf("收件地址 = %q", messages[0].To)
	}
	if !strings.HasPrefix(messages[0].Subject, "【易班签到】") ||
		!strings.Contains(messages[0].Text, "此邮件由系统自动发送，请勿回复。") {
		t.Fatalf("邮件模板 = %q / %q", messages[0].Subject, messages[0].Text)
	}
	if strings.Contains(messages[0].Subject, phone) || strings.Contains(messages[0].Text, phone) ||
		mask.TextHasPhone(messages[0].Subject) || mask.TextHasPhone(messages[0].Text) {
		t.Fatalf("邮件文本含完整手机号：%q / %q", messages[0].Subject, messages[0].Text)
	}
	if strings.Contains(logs.String(), phone) || strings.Contains(logs.String(), email) {
		t.Fatalf("日志含完整标识：%s", logs.String())
	}
}

// 缺业务日、非法业务日、缺摘要端口都在用例层拒绝。
func TestDispatchDigestValidatesInput(t *testing.T) {
	store := newFakeStore()
	dispatcher := newDigest(t, store, newFakeDigestSource(), &fakeDigestAudience{},
		&fakeSender{}, newManualClock(), nil)
	if err := dispatcher.DispatchDigest(context.Background(), ""); errs.From(err).Code != errs.CodeValidation {
		t.Fatalf("缺业务日 code = %v", errs.From(err).Code)
	}
	if err := dispatcher.DispatchDigest(context.Background(), "29/10/2026"); errs.From(err).Code != errs.CodeValidation {
		t.Fatalf("非法业务日 code = %v", errs.From(err).Code)
	}
	// 未注入摘要端口的 dispatcher：其余投递照常，摘要显式失败。
	withoutDigest, err := appnotify.NewDispatcher(appnotify.DispatcherDeps{
		Notes: store, Ledger: store, Audience: &fakeAudience{},
		Clock: newManualClock(),
	})
	if err != nil {
		t.Fatalf("NewDispatcher: %v", err)
	}
	if err := withoutDigest.DispatchDigest(context.Background(), digestDay); err == nil {
		t.Fatal("缺摘要端口必须报错，不得静默跳过")
	}
}
