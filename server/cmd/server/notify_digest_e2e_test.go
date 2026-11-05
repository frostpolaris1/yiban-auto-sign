package main

// 0v8 C 每日签到失败摘要的真库 e2e：Dispatcher + 真 PostgreSQL + 假 SMTP。
//
// 覆盖：master 全量 / admin 绑定校 / 无绑定 admin 不发；
// 空日不发；软删账号与别的业务日不进摘要；
// 同账号多任务聚合（最新 result、最大 attempts）；
// 同收件人同日重复触发一行通知、一条账本、一封邮件；
// users.mail_notify=false 不影响摘要（两开关不 OR）；
// prefs 显式关 email 不发；收信正文已掩码。

import (
	"io"
	"log/slog"
	"strings"
	"testing"
	"time"

	mailadapter "github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/mail"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/mail/mailtest"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	appnotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/notify"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coremail "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mail"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
)

// schoolTask 建一条院校任务，返回其标识。
func (e *notifyDeliveryEnv) schoolTask(schoolID, name string) string {
	e.t.Helper()
	return e.scalarString(
		`INSERT INTO school_tasks (school_id, name) VALUES ($1::uuid, $2) RETURNING id::text`,
		schoolID, name)
}

// signTask 直接写一行 sign_tasks。
func (e *notifyDeliveryEnv) signTask(accountID, schoolTaskID, day, state, result string,
	attempts int, at time.Time) {
	e.t.Helper()
	if _, err := e.db.Exec(e.ctx, `
		INSERT INTO sign_tasks
		       (account_id, school_task_id, day, run_at, state, attempts, result, updated_at)
		VALUES ($1::uuid, $2::uuid, $3::date, $4::timestamptz, $5::sign_task_state, $6, $7, $8::timestamptz)`,
		accountID, schoolTaskID, day, at, state, attempts, result, at); err != nil {
		e.t.Fatalf("insert sign task %s/%s: %v", accountID, day, err)
	}
}

// digestDispatcher 用真库仓储与摘要端口构造投递服务。
func (e *notifyDeliveryEnv) digestDispatcher(mail coremail.Sender,
	clk *notifyTestClock) *appnotify.Dispatcher {
	e.t.Helper()
	repo := pg.NewNotificationDigestRepo(e.db)
	dispatcher, err := appnotify.NewDispatcher(appnotify.DispatcherDeps{
		Notes:          pg.NewNotificationRepo(e.db),
		Ledger:         pg.NewNotificationDeliveryRepo(e.db),
		Audience:       pg.NewNotificationAudienceRepo(e.db),
		DigestSource:   repo,
		DigestAudience: repo,
		Mail:           mail,
		Clock:          clk,
		Logger:         slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if err != nil {
		e.t.Fatalf("NewDispatcher: %v", err)
	}
	return dispatcher
}

// ledgerCount 数某收件人某业务键下的账本行。
func (e *notifyDeliveryEnv) ledgerCount(userID, dedupe string) int64 {
	e.t.Helper()
	return e.scalarInt(`
		SELECT count(*) FROM notification_deliveries d
		  JOIN notifications n ON n.id = d.notification_id
		 WHERE n.user_id = $1::uuid AND n.dedupe_key = $2`, userID, dedupe)
}

// 摘要全链路：scope、聚合、去重、账本唯一键、软删排除、两开关不 OR 与收信。
func TestNotifyDigestEndToEnd(t *testing.T) {
	e := newNotifyDeliveryEnv(t)
	schoolA := e.school("digest-a")
	schoolB := e.school("digest-b")
	master := e.admin("digest-master@example.com", coreauth.RoleMaster)
	adminA := e.admin("digest-admin-a@example.com", coreauth.RoleAdmin, schoolA)
	adminB := e.admin("digest-admin-b@example.com", coreauth.RoleAdmin, schoolB)
	unbound := e.admin("digest-unbound@example.com", coreauth.RoleAdmin)
	owner := e.user("digest-owner@example.com", coreauth.RoleUser)
	first := e.account(schoolA, owner.ID, "13800000001")
	second := e.account(schoolB, owner.ID, "13800000002")
	gone := e.account(schoolA, owner.ID, "13800000003")
	taskA := e.schoolTask(schoolA, "digest-a-1")
	taskA2 := e.schoolTask(schoolA, "digest-a-2")
	taskB := e.schoolTask(schoolB, "digest-b-1")

	const day = "2026-10-29"
	early := time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)
	e.signTask(first, taskA, day, string(corestatus.TaskFailed), "blocked", 2, early)
	e.signTask(first, taskA2, day, string(corestatus.TaskFailed), "window_closed", 5, early.Add(5*time.Minute))
	e.signTask(second, taskB, day, string(corestatus.TaskFailed), "blocked", 1, early)
	// 软删账号与别的业务日都不进摘要。
	e.signTask(gone, taskA, day, string(corestatus.TaskFailed), "blocked", 1, early)
	e.signTask(first, taskA, "2026-10-28", string(corestatus.TaskFailed), "blocked", 9, early)
	if _, err := e.db.Exec(e.ctx,
		"UPDATE accounts SET deleted_at = now() WHERE id = $1::uuid", gone); err != nil {
		t.Fatalf("soft delete account: %v", err)
	}
	// 摘要不看 users.mail_notify：master 显式关掉仍要收。
	if err := e.users.SetMailNotify(e.ctx, master.ID, false); err != nil {
		t.Fatalf("set mail notify: %v", err)
	}
	// adminB 显式关掉 email：in_app 缺行按默认 false，整条跳过。
	e.setPrefs(adminB.ID,
		corenotify.Pref{
			Category: corenotify.CategorySignFailedDaily,
			Channel:  corenotify.ChannelEmail, Enabled: false,
		})

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
	dispatcher := e.digestDispatcher(sender, clk)

	// 同日重复触发：唯一键吸收，不产生第二份。
	for i := 0; i < 2; i++ {
		if err := dispatcher.DispatchDigest(e.ctx, day); err != nil {
			t.Fatalf("DispatchDigest: %v", err)
		}
	}
	dedupe := "sign_failed_daily/" + day
	for _, userID := range []string{master.ID, adminA.ID} {
		if got := e.notificationCount(userID, dedupe); got != 1 {
			t.Fatalf("通知(%s) = %d, want 1", userID, got)
		}
		if got := e.ledgerCount(userID, dedupe); got != 1 {
			t.Fatalf("账本(%s) = %d, want 1（唯一键）", userID, got)
		}
	}
	for _, userID := range []string{adminB.ID, unbound.ID} {
		if got := e.notificationCount(userID, dedupe); got != 0 {
			t.Fatalf("通知(%s) = %d, want 0", userID, got)
		}
		if got := e.ledgerCount(userID, dedupe); got != 0 {
			t.Fatalf("账本(%s) = %d, want 0", userID, got)
		}
	}

	masterBody := e.scalarString(
		`SELECT body FROM notifications WHERE user_id = $1::uuid AND dedupe_key = $2`,
		master.ID, dedupe)
	if !strings.Contains(masterBody, day+" 共有 2 个账号签到失败。") {
		t.Fatalf("master 首行 = %q", masterBody)
	}
	for _, masked := range []string{"138****0001", "138****0002"} {
		if !strings.Contains(masterBody, masked) {
			t.Fatalf("master 正文缺 %s：%q", masked, masterBody)
		}
	}
	if strings.Contains(masterBody, "138****0003") {
		t.Fatalf("软删账号进了摘要：%q", masterBody)
	}
	if !strings.Contains(masterBody, "window_closed（尝试 5 次）") {
		t.Fatalf("未取最新 result / 最大 attempts：%q", masterBody)
	}
	if strings.Contains(masterBody, "13800000001") || mask.TextHasPhone(masterBody) {
		t.Fatalf("正文含完整手机号：%q", masterBody)
	}
	adminBody := e.scalarString(
		`SELECT body FROM notifications WHERE user_id = $1::uuid AND dedupe_key = $2`,
		adminA.ID, dedupe)
	if !strings.Contains(adminBody, day+" 共有 1 个账号签到失败。") ||
		!strings.Contains(adminBody, "138****0001") ||
		strings.Contains(adminBody, "138****0002") {
		t.Fatalf("admin 正文 = %q", adminBody)
	}

	// 投递一轮：master + adminA 各一封，无第二封。
	if err := dispatcher.SendDue(e.ctx); err != nil {
		t.Fatalf("SendDue: %v", err)
	}
	if !srv.WaitMessages(2, 3*time.Second) {
		t.Fatalf("假服务器收到 %d 封，want 2", srv.MessageCount())
	}
	recipients := map[string]int{}
	for _, msg := range srv.Messages() {
		recipients[msg.To[0]]++
		if strings.Contains(msg.Data, "13800000001") || mask.TextHasPhone(msg.Data) {
			t.Fatalf("邮件正文含完整手机号：%q", msg.Data)
		}
	}
	if len(recipients) != 2 || recipients["digest-master@example.com"] != 1 ||
		recipients["digest-admin-a@example.com"] != 1 {
		t.Fatalf("收件人 = %v, want master/adminA 各一封", recipients)
	}
	if got := e.scalarInt(`SELECT count(*) FROM notification_deliveries WHERE status = 'sent'`); got != 2 {
		t.Fatalf("sent 行 = %d, want 2", got)
	}

	// 空日不发：没有任何新行。
	before := e.scalarInt(`SELECT count(*) FROM notifications`)
	if err := dispatcher.DispatchDigest(e.ctx, "2026-10-27"); err != nil {
		t.Fatalf("DispatchDigest(empty): %v", err)
	}
	if after := e.scalarInt(`SELECT count(*) FROM notifications`); after != before {
		t.Fatalf("空日新增 %d 行通知", after-before)
	}
	if e.notificationCount(unbound.ID, dedupe) != 0 {
		t.Fatal("无绑定校 admin 收到摘要")
	}
}
