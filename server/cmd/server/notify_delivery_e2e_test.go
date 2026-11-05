package main

// 0v8 B 通知投递链路的真库 e2e：Dispatcher + 真 PostgreSQL + 假 SMTP。
//
// 覆盖：管理员收件人按院校展开、默认订阅与显式覆盖、重复事件只落一行、
// 用户失败邮件只看 users.mail_notify、容量与待审核事件的幂等键、
// 重试账本的 1m/5m/30m 档位与终态、SMTP 未配置落 skipped，
// 以及经真实 SMTP 适配器 + mailtest 的收信与掩码断言。
// 收件人解析的 SQL 面在 adapters/pg 的 e2e；本文件打完整用例链路。

import (
	"context"
	"io"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	mailadapter "github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/mail"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/mail/mailtest"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	appnotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/notify"
	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coremail "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mail"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// notifyTestClock 是可推进的墙钟：重试档位按分钟计，测试直接拨表。
type notifyTestClock struct {
	mu  sync.Mutex
	now time.Time
}

func newNotifyTestClock() *notifyTestClock {
	return &notifyTestClock{now: time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)}
}

func (c *notifyTestClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *notifyTestClock) Advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.now = c.now.Add(d)
}

// notifyDeliveryEnv 独占一个 schema 的真库环境。
type notifyDeliveryEnv struct {
	t     *testing.T
	ctx   context.Context
	db    *pg.DB
	users *pg.UserRepo
	accts *pg.AccountRepo
}

func newNotifyDeliveryEnv(t *testing.T) *notifyDeliveryEnv {
	t.Helper()
	base := w2cRequireDSN(t)
	ctx := t.Context()
	schema := "notify_b_" + w2cRandomHex(6)

	adminDB, err := pg.Open(ctx, pg.Options{DSN: base, MaxConns: 1, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open admin connection: %v", err)
	}
	if _, err := adminDB.Exec(ctx, "CREATE SCHEMA "+schema); err != nil {
		adminDB.Close()
		t.Fatalf("create schema %s: %v", schema, err)
	}
	dsn := w2cWithSchema(base, schema)
	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 4, StatementTimeout: 10 * time.Second})
	if err != nil {
		w2cDropSchema(adminDB, schema)
		adminDB.Close()
		t.Fatalf("open test connection: %v", err)
	}
	if _, err := pg.Migrate(ctx, dsn); err != nil {
		db.Close()
		w2cDropSchema(adminDB, schema)
		adminDB.Close()
		t.Fatalf("migrate test schema: %v", err)
	}
	t.Cleanup(func() {
		db.Close()
		w2cDropSchema(adminDB, schema)
		adminDB.Close()
	})
	return &notifyDeliveryEnv{t: t, ctx: ctx, db: db, users: pg.NewUserRepo(db), accts: pg.NewAccountRepo(db)}
}

// user 建一个用户；role 非空时一并写角色。
func (e *notifyDeliveryEnv) user(email string, role coreauth.Role) coreuser.Record {
	e.t.Helper()
	var rec coreuser.Record
	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		var err error
		rec, err = e.users.Create(ctx, coreuser.New{
			Email: email, PasswordHash: "argon2id$test", Role: role,
		}, 10000)
		return err
	})
	if err != nil {
		e.t.Fatalf("create user %s: %v", email, err)
	}
	return rec
}

// admin 建管理员并按需绑定院校。
func (e *notifyDeliveryEnv) admin(email string, role coreauth.Role, schools ...string) coreuser.Record {
	e.t.Helper()
	rec := e.user(email, role)
	for _, schoolID := range schools {
		if _, err := e.db.Exec(e.ctx,
			"INSERT INTO admin_schools (user_id, school_id) VALUES ($1::uuid, $2::uuid)",
			rec.ID, schoolID); err != nil {
			e.t.Fatalf("bind school %s: %v", schoolID, err)
		}
	}
	return rec
}

// school 建一所院校。
func (e *notifyDeliveryEnv) school(code string) string {
	e.t.Helper()
	id := e.scalarString(
		`INSERT INTO schools (code, name) VALUES ($1, $2) RETURNING id::text`,
		code, "School "+code)
	return id
}

// account 建一个挂在校下的账号。
func (e *notifyDeliveryEnv) account(schoolID, ownerID, phone string) string {
	e.t.Helper()
	var rec coreaccount.Record
	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		var err error
		rec, err = e.accts.Create(ctx, coreaccount.New{
			SchoolID: schoolID, OwnerUserID: ownerID, Phone: phone,
		}, 10000, 10000)
		return err
	})
	if err != nil {
		e.t.Fatalf("create account %s: %v", phone, err)
	}
	return rec.ID
}

// scalarString 读一行文本标量。
func (e *notifyDeliveryEnv) scalarString(sqlText string, args ...any) string {
	e.t.Helper()
	out, err := e.db.QueryString(e.ctx, sqlText, args...)
	if err != nil {
		e.t.Fatalf("query %q: %v", sqlText, err)
	}
	return out
}

// scalarInt 读一行整数标量。
func (e *notifyDeliveryEnv) scalarInt(sqlText string, args ...any) int64 {
	e.t.Helper()
	out, err := e.db.QueryInt64(e.ctx, sqlText, args...)
	if err != nil {
		e.t.Fatalf("query %q: %v", sqlText, err)
	}
	return out
}

// setPrefs 用真实的订阅仓储写入覆盖集。
func (e *notifyDeliveryEnv) setPrefs(userID string, prefs ...corenotify.Pref) {
	e.t.Helper()
	if err := pg.NewNotificationPrefsRepo(e.db).Replace(e.ctx, userID, prefs); err != nil {
		e.t.Fatalf("replace prefs: %v", err)
	}
}

// notificationCount 数某用户某业务键的通知行。
func (e *notifyDeliveryEnv) notificationCount(userID, dedupe string) int64 {
	e.t.Helper()
	return e.scalarInt(
		`SELECT count(*) FROM notifications WHERE user_id = $1::uuid AND dedupe_key = $2`,
		userID, dedupe)
}

// dispatcher 用真库仓储构造投递服务。
func (e *notifyDeliveryEnv) dispatcher(mail coremail.Sender, clk *notifyTestClock) *appnotify.Dispatcher {
	e.t.Helper()
	dispatcher, err := appnotify.NewDispatcher(appnotify.DispatcherDeps{
		Notes:    pg.NewNotificationRepo(e.db),
		Ledger:   pg.NewNotificationDeliveryRepo(e.db),
		Audience: pg.NewNotificationAudienceRepo(e.db),
		Mail:     mail,
		Clock:    clk,
		Logger:   slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if err != nil {
		e.t.Fatalf("NewDispatcher: %v", err)
	}
	return dispatcher
}

// deliveryStateFromDB 读某用户某业务键下的唯一账本行状态。
func (e *notifyDeliveryEnv) deliveryStateFromDB(userID, dedupe string) (string, int64, string, string, string) {
	e.t.Helper()
	id := e.scalarString(
		`SELECT d.id::text FROM notification_deliveries d
		   JOIN notifications n ON n.id = d.notification_id
		  WHERE n.user_id = $1::uuid AND n.dedupe_key = $2`, userID, dedupe)
	return e.deliveryState(id)
}

// deliveryState 读一行的 (status, attempts, last_error_kind, skip_reason, next_retry_at)。
func (e *notifyDeliveryEnv) deliveryState(id string) (string, int64, string, string, string) {
	e.t.Helper()
	status := e.scalarString(
		`SELECT status FROM notification_deliveries WHERE id = $1::uuid`, id)
	attempts := e.scalarInt(
		`SELECT attempts FROM notification_deliveries WHERE id = $1::uuid`, id)
	kind := e.scalarString(
		`SELECT last_error_kind FROM notification_deliveries WHERE id = $1::uuid`, id)
	skip := e.scalarString(
		`SELECT skip_reason FROM notification_deliveries WHERE id = $1::uuid`, id)
	next := e.scalarString(
		`SELECT coalesce(next_retry_at::text, '') FROM notification_deliveries WHERE id = $1::uuid`, id)
	return status, attempts, kind, skip, next
}

// 管理员事件全链路：收件人按院校展开、默认态只有站内信、
// 显式开 email 后经真实 SMTP 适配器投递一封、重复事件仍各一行。
func TestNotifyDeliveryDeliversToScopedAdmins(t *testing.T) {
	e := newNotifyDeliveryEnv(t)
	schoolA := e.school("deliver-a")
	schoolB := e.school("deliver-b")
	master := e.admin("deliver-master@example.com", coreauth.RoleMaster)
	adminA := e.admin("deliver-admin-a@example.com", coreauth.RoleAdmin, schoolA)
	adminB := e.admin("deliver-admin-b@example.com", coreauth.RoleAdmin, schoolB)
	owner := e.user("deliver-owner@example.com", coreauth.RoleUser)
	account := e.account(schoolA, owner.ID, "13800000001")

	srv, err := mailtest.Start(mailtest.StartTLS)
	if err != nil {
		t.Fatalf("start fake smtp: %v", err)
	}
	t.Cleanup(srv.Close)
	sender, err := mailadapter.NewSMTP(mailadapter.Config{
		Host: "127.0.0.1", Port: srv.Port(), From: "notify@example.test",
		TLSMode: coremail.TLSModeStartTLS, Timeout: 3 * time.Second, TLSConfig: srv.TLSConfig(),
	})
	if err != nil {
		t.Fatalf("NewSMTP: %v", err)
	}
	clk := newNotifyTestClock()
	dispatcher := e.dispatcher(sender, clk)

	dedupe := "credential_invalid/" + account + "/2026-10-29"
	event := corenotify.Event{
		Category:  corenotify.CategoryCredentialInvalid,
		SchoolID:  schoolA,
		AccountID: account,
		DedupeKey: dedupe,
		Day:       "2026-10-29",
	}
	// 默认态：credential_invalid 只进站内信（in_app 开、email 关）。
	if err := dispatcher.Dispatch(e.ctx, event); err != nil {
		t.Fatalf("Dispatch: %v", err)
	}
	for _, userID := range []string{master.ID, adminA.ID} {
		if got := e.notificationCount(userID, dedupe); got != 1 {
			t.Fatalf("通知行(%s) = %d, want 1", userID, got)
		}
	}
	if got := e.notificationCount(adminB.ID, dedupe); got != 0 {
		t.Fatalf("跨校 admin 收到 %d 行", got)
	}
	if got := e.scalarInt(
		`SELECT count(*) FROM notification_deliveries WHERE notification_id IN
		   (SELECT id FROM notifications WHERE dedupe_key = $1)`, dedupe); got != 0 {
		t.Fatalf("默认态账本行 = %d, want 0", got)
	}

	// 显式订阅 email：同一业务键的重复事件只为已有通知补账本行。
	for _, userID := range []string{master.ID, adminA.ID} {
		e.setPrefs(userID, corenotify.Pref{
			Category: corenotify.CategoryCredentialInvalid, Channel: corenotify.ChannelEmail, Enabled: true,
		})
	}
	for i := 0; i < 2; i++ {
		if err := dispatcher.Dispatch(e.ctx, event); err != nil {
			t.Fatalf("Dispatch: %v", err)
		}
	}
	if got := e.scalarInt(
		`SELECT count(*) FROM notification_deliveries WHERE notification_id IN
		   (SELECT id FROM notifications WHERE dedupe_key = $1)`, dedupe); got != 2 {
		t.Fatalf("账本行 = %d, want 2（master + adminA）", got)
	}
	if got := e.notificationCount(master.ID, dedupe); got != 1 {
		t.Fatalf("重复事件通知 = %d, want 1", got)
	}

	// 投递一轮：两封都到假服务器，正文已掩码。
	if err := dispatcher.SendDue(e.ctx); err != nil {
		t.Fatalf("SendDue: %v", err)
	}
	if !srv.WaitMessages(2, 3*time.Second) {
		t.Fatalf("假服务器收到 %d 封，want 2", srv.MessageCount())
	}
	recipients := map[string]bool{}
	for _, msg := range srv.Messages() {
		recipients[msg.To[0]] = true
		if !strings.Contains(msg.Data, "Subject:") {
			t.Fatalf("缺 Subject 头：%q", msg.Data)
		}
		if !strings.Contains(msg.Data, "138****0001") {
			t.Fatalf("正文缺掩码手机号：%q", msg.Data)
		}
		if strings.Contains(msg.Data, "13800000001") || mask.TextHasPhone(msg.Data) {
			t.Fatalf("正文含完整手机号：%q", msg.Data)
		}
	}
	if !recipients["deliver-master@example.com"] || !recipients["deliver-admin-a@example.com"] {
		t.Fatalf("收件人 = %v", recipients)
	}
	if got := e.scalarInt(
		`SELECT count(*) FROM notification_deliveries WHERE status = 'sent'`); got != 2 {
		t.Fatalf("sent 行 = %d, want 2", got)
	}
}

// 用户失败邮件只看 users.mail_notify；同账号同日重复失败仍一行。
func TestNotifyDeliveryUserFailureHonorsMailNotify(t *testing.T) {
	e := newNotifyDeliveryEnv(t)
	school := e.school("user-fail")
	owner := e.user("user-fail-owner@example.com", coreauth.RoleUser)
	account := e.account(school, owner.ID, "13800000001")
	dispatcher := e.dispatcher(nil, newNotifyTestClock())

	failure := corenotify.UserFailure{
		SchoolID: school, AccountID: account, Day: "2026-10-29",
		Outcome: "credential_invalid", Reason: "登录失败",
	}
	// 开关默认是 true（从未改过的用户读 true）；显式关掉后不写任何行。
	if err := e.users.SetMailNotify(e.ctx, owner.ID, false); err != nil {
		t.Fatalf("set mail notify: %v", err)
	}
	if err := dispatcher.DispatchUserFailure(e.ctx, failure); err != nil {
		t.Fatalf("DispatchUserFailure: %v", err)
	}
	dedupe := "user_fail_mail/" + account + "/2026-10-29"
	if got := e.notificationCount(owner.ID, dedupe); got != 0 {
		t.Fatalf("mail_notify=false 通知 = %d, want 0", got)
	}
	if got := e.scalarInt(`SELECT count(*) FROM notification_deliveries`); got != 0 {
		t.Fatalf("mail_notify=false 账本 = %d, want 0", got)
	}

	if err := e.users.SetMailNotify(e.ctx, owner.ID, true); err != nil {
		t.Fatalf("set mail notify: %v", err)
	}
	for i := 0; i < 2; i++ {
		if err := dispatcher.DispatchUserFailure(e.ctx, failure); err != nil {
			t.Fatalf("DispatchUserFailure: %v", err)
		}
	}
	if got := e.notificationCount(owner.ID, dedupe); got != 1 {
		t.Fatalf("用户失败通知 = %d, want 1", got)
	}
	category := e.scalarString(
		`SELECT category FROM notifications WHERE user_id = $1::uuid AND dedupe_key = $2`,
		owner.ID, dedupe)
	if category != string(corenotify.CategorySignFailedDaily) {
		t.Fatalf("类别 = %q", category)
	}
	// SMTP 未配置：账本行落 skipped，不留给重试循环空转。
	status, attempts, kind, skip, next := e.deliveryStateFromDB(owner.ID, dedupe)
	if status != corenotify.DeliveryStatusSkipped || attempts != 0 || kind != "" ||
		skip != "smtp_not_configured" || next != "" {
		t.Fatalf("账本 = %s/%d/%q/%q/%q", status, attempts, kind, skip, next)
	}
	// 到期扫描不返回 skipped 行。
	due, err := pg.NewNotificationDeliveryRepo(e.db).ListDueForSend(e.ctx,
		time.Date(2026, 10, 30, 0, 0, 0, 0, time.UTC), 10)
	if err != nil {
		t.Fatalf("ListDueForSend: %v", err)
	}
	if len(due) != 0 {
		t.Fatalf("skipped 行被当成到期：%+v", due)
	}
}

// 容量事件：同任务同日重复触发只落一行通知、一条账本行；默认不发信。
func TestNotifyDeliveryCapacityRejectedIdempotent(t *testing.T) {
	e := newNotifyDeliveryEnv(t)
	school := e.school("cap")
	master := e.admin("cap-master@example.com", coreauth.RoleMaster)
	// 显式开 email：重复缺口仍只一条账本行。
	e.setPrefs(master.ID, corenotify.Pref{
		Category: corenotify.CategoryCapacityRejected, Channel: corenotify.ChannelEmail, Enabled: true,
	})
	dispatcher := e.dispatcher(nil, newNotifyTestClock())
	event := corenotify.Event{
		Category:  corenotify.CategoryCapacityRejected,
		SchoolID:  school,
		DedupeKey: "capacity_rejected/task-1/2026-10-29",
		Day:       "2026-10-29",
		Capacity: corenotify.CapacityFigures{
			Demand: 500, Capacity: 120, Shortfall: 380, Binding: "workers", SuggestedWorkers: 8,
		},
	}
	for i := 0; i < 2; i++ {
		if err := dispatcher.Dispatch(e.ctx, event); err != nil {
			t.Fatalf("Dispatch: %v", err)
		}
	}
	if got := e.notificationCount(master.ID, event.DedupeKey); got != 1 {
		t.Fatalf("容量通知 = %d, want 1", got)
	}
	body := e.scalarString(
		`SELECT body FROM notifications WHERE user_id = $1::uuid AND dedupe_key = $2`,
		master.ID, event.DedupeKey)
	if !strings.Contains(body, "需求 500") || !strings.Contains(body, "缺口 380") {
		t.Fatalf("容量正文 = %q", body)
	}
	// SMTP 未配置：账本行落 skipped，但只有一条。
	status, attempts, kind, skip, _ := e.deliveryStateFromDB(master.ID, event.DedupeKey)
	if status != corenotify.DeliveryStatusSkipped || attempts != 0 || kind != "" ||
		skip != "smtp_not_configured" {
		t.Fatalf("容量账本 = %s/%d/%q/%q", status, attempts, kind, skip)
	}
	if got := e.scalarInt(`SELECT count(*) FROM notification_deliveries`); got != 1 {
		t.Fatalf("容量账本行 = %d, want 1", got)
	}
}

// 待审核事件每次提交都是新业务键：两行通知，不是一行。
func TestNotifyDeliveryReviewPendingKeepsBothEvents(t *testing.T) {
	e := newNotifyDeliveryEnv(t)
	school := e.school("review")
	master := e.admin("review-master@example.com", coreauth.RoleMaster)
	owner := e.user("review-owner@example.com", coreauth.RoleUser)
	account := e.account(school, owner.ID, "13800000001")
	dispatcher := e.dispatcher(nil, newNotifyTestClock())

	for _, key := range []string{
		"review_pending/" + account + "/event-1",
		"review_pending/" + account + "/event-2",
	} {
		if err := dispatcher.Dispatch(e.ctx, corenotify.Event{
			Category: corenotify.CategoryReviewPending, SchoolID: school,
			AccountID: account, DedupeKey: key,
		}); err != nil {
			t.Fatalf("Dispatch: %v", err)
		}
	}
	if got := e.scalarInt(
		`SELECT count(*) FROM notifications WHERE user_id = $1::uuid
		   AND category = 'review_pending'`, master.ID); got != 2 {
		t.Fatalf("待审核通知 = %d, want 2", got)
	}
}

// 重试账本：可重试错误按 1m/5m/30m 记 next_retry_at，第 4 次失败落 failed。
func TestNotifyDeliveryRetryLedger(t *testing.T) {
	e := newNotifyDeliveryEnv(t)
	school := e.school("retry")
	master := e.admin("retry-master@example.com", coreauth.RoleMaster)
	owner := e.user("retry-owner@example.com", coreauth.RoleUser)
	account := e.account(school, owner.ID, "13800000001")
	e.setPrefs(master.ID, corenotify.Pref{
		Category: corenotify.CategoryReviewPending, Channel: corenotify.ChannelEmail, Enabled: true,
	})
	clk := newNotifyTestClock()
	sender := &scriptedMailSender{
		errors: []error{
			&coremail.DeliveryError{Kind: coremail.KindConnect, Retryable: true},
			&coremail.DeliveryError{Kind: coremail.KindTimeout, Retryable: true},
			&coremail.DeliveryError{Kind: coremail.KindConnect, Retryable: true},
			&coremail.DeliveryError{Kind: coremail.KindConnect, Retryable: true},
		},
	}
	dispatcher := e.dispatcher(sender, clk)

	dedupe := "review_pending/" + account + "/event-1"
	if err := dispatcher.Dispatch(e.ctx, corenotify.Event{
		Category: corenotify.CategoryReviewPending, SchoolID: school,
		AccountID: account, DedupeKey: dedupe,
	}); err != nil {
		t.Fatalf("Dispatch: %v", err)
	}
	deliveryID := e.scalarString(
		`SELECT d.id::text FROM notification_deliveries d
		   JOIN notifications n ON n.id = d.notification_id
		  WHERE n.user_id = $1::uuid AND n.dedupe_key = $2`, master.ID, dedupe)

	wantDelays := []time.Duration{time.Minute, 5 * time.Minute, 30 * time.Minute}
	for attempt, delay := range wantDelays {
		if err := dispatcher.SendDue(e.ctx); err != nil {
			t.Fatalf("SendDue: %v", err)
		}
		status, attempts, kind, skip, next := e.deliveryState(deliveryID)
		if status != corenotify.DeliveryStatusPending || attempts != int64(attempt+1) ||
			kind == "" || skip != "" || next == "" {
			t.Fatalf("第 %d 次失败 = %s/%d/%q/%q/%q", attempt+1, status, attempts, kind, skip, next)
		}
		onTime := e.scalarString(
			`SELECT (next_retry_at = $2::timestamptz)::text
			   FROM notification_deliveries WHERE id = $1::uuid`,
			deliveryID, clk.Now().Add(delay))
		if onTime != "true" {
			t.Fatalf("第 %d 次 next_retry_at = %q, want +%s", attempt+1, next, delay)
		}
		clk.Advance(delay)
	}
	if err := dispatcher.SendDue(e.ctx); err != nil {
		t.Fatalf("SendDue: %v", err)
	}
	status, attempts, kind, skip, next := e.deliveryState(deliveryID)
	if status != corenotify.DeliveryStatusFailed || attempts != 4 || kind != "connect" || skip != "" || next != "" {
		t.Fatalf("终态 = %s/%d/%q/%q/%q", status, attempts, kind, skip, next)
	}
	if sender.calls != 4 {
		t.Fatalf("投递次数 = %d, want 4", sender.calls)
	}
}

// scriptedMailSender 按脚本返回失败，记录调用次数。
type scriptedMailSender struct {
	mu     sync.Mutex
	errors []error
	calls  int
}

func (s *scriptedMailSender) Send(context.Context, coremail.Message) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.calls++
	if len(s.errors) == 0 {
		return nil
	}
	err := s.errors[0]
	s.errors = s.errors[1:]
	return err
}
