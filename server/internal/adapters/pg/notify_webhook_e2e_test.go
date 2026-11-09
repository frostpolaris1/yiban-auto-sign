package pg_test

// 2d webhook 渠道的真库 e2e：账本行的写读与当日额度计数。
//
// 覆盖：webhook 账本行可写可读（同事务路径与唯一键吸收）；
// in_app 渠道仍拒建账本行；CountSentOnDay 按 updated_at 窗口计数、
// 跨日归零、failed/skipped 不计数。投递用例链路的 e2e 在 cmd/server。

import (
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
)

// webhook 账本行：email 与 webhook 各一行挂同一通知；
// (notification_id, channel, user_id) 按渠道各自吸收重复入队；
// in_app 仍拒建账本行。
func TestNotificationWebhookLedgerRows(t *testing.T) {
	e := newEnv(t)
	user := e.createUser("wh-ledger@example.com")
	notes := pg.NewNotificationRepo(e.db)
	ledger := pg.NewNotificationDeliveryRepo(e.db)
	now := time.Date(2026, 11, 10, 6, 0, 0, 0, time.UTC)

	note := corenotify.NotificationInsert{
		UserID: user.ID, Category: corenotify.CategoryReviewPending,
		DedupeKey: "wh/1", Title: "账号待审核", Body: "body", CreatedAt: now,
	}
	emailRow, err := notes.InsertWithPending(e.ctx, note, corenotify.DeliveryInsert{
		Channel: corenotify.ChannelEmail, NextRetryAt: &now,
	})
	if err != nil {
		t.Fatalf("email pending: %v", err)
	}
	webhookRow, err := notes.InsertWithPending(e.ctx, note, corenotify.DeliveryInsert{
		Channel: corenotify.ChannelWebhook, NextRetryAt: &now,
	})
	if err != nil {
		t.Fatalf("webhook pending: %v", err)
	}
	// 通知行去重命中返回同一行；账本行按渠道各一条。
	if webhookRow.Notification.ID != emailRow.Notification.ID {
		t.Fatalf("第二个渠道重建了通知行: %s vs %s",
			emailRow.Notification.ID, webhookRow.Notification.ID)
	}
	if webhookRow.Delivery.ID == emailRow.Delivery.ID {
		t.Fatal("webhook 行与 email 行必须是两行账本")
	}
	if got := e.scalarInt(
		`SELECT count(*) FROM notification_deliveries WHERE notification_id = $1::uuid`,
		emailRow.Notification.ID); got != 2 {
		t.Fatalf("账本行 = %d, want 2", got)
	}
	// webhook 行重复入队：唯一键吸收。
	again, err := notes.InsertWithPending(e.ctx, note, corenotify.DeliveryInsert{
		Channel: corenotify.ChannelWebhook, NextRetryAt: &now,
	})
	if err != nil || again.Delivery.ID != webhookRow.Delivery.ID {
		t.Fatalf("重复入队 = (%+v,%v)，want 同一行 %s", again, err, webhookRow.Delivery.ID)
	}
	if got := e.scalarInt(
		`SELECT count(*) FROM notification_deliveries WHERE notification_id = $1::uuid`,
		emailRow.Notification.ID); got != 2 {
		t.Fatalf("重放后账本行 = %d, want 2", got)
	}
	// 到期扫描带出 webhook 行（正文同源，无邮箱）。
	due, err := ledger.ListDueForSend(e.ctx, now.Add(time.Minute), 10)
	if err != nil {
		t.Fatalf("ListDueForSend: %v", err)
	}
	channels := map[corenotify.Channel]int{}
	for _, row := range due {
		channels[row.Channel]++
		// DueDelivery.Email 是共用读模型的联系信息列，webhook 行不消费它
		//（投递用例不读 webhook 行的 Email）；此处只钉渠道分布。
	}
	if channels[corenotify.ChannelWebhook] != 1 || channels[corenotify.ChannelEmail] != 1 {
		t.Fatalf("到期渠道分布 = %v, want email 1 + webhook 1", channels)
	}
	// in_app 仍拒建账本行（站内信渠道无投递账本）。
	if _, err := ledger.InsertPending(e.ctx, corenotify.DeliveryInsert{
		NotificationID: emailRow.Notification.ID, UserID: user.ID,
		Channel: corenotify.ChannelInApp, NextRetryAt: &now,
	}); err == nil {
		t.Fatal("in_app 渠道不应可建账本行")
	}
}

// CountSentOnDay：按 updated_at 窗口计数；跨日归零；
// failed 与 skipped 行不计数。
func TestNotificationCountSentOnDayWindows(t *testing.T) {
	e := newEnv(t)
	user := e.createUser("wh-budget@example.com")
	notes := pg.NewNotificationRepo(e.db)
	ledger := pg.NewNotificationDeliveryRepo(e.db)
	// 窗口从库时钟派生：MarkSent 的 updated_at=now() 写的是 pg 时钟，
	// 测试窗口必须与它同源（测试进程时钟在容器里可能漂移）。
	// epoch 秒数规避 timestamptz 文本形态的时区偏移差异（+00 与 +00:00）。
	now := epochTime(t, e.scalarString(`SELECT extract(epoch from now())::text`))

	seed := func(dedupe string, channel corenotify.Channel) string {
		t.Helper()
		note, err := notes.Insert(e.ctx, corenotify.NotificationInsert{
			UserID: user.ID, Category: corenotify.CategoryReviewPending,
			DedupeKey: dedupe, Title: "t", Body: "b", CreatedAt: now,
		})
		if err != nil {
			t.Fatalf("seed note: %v", err)
		}
		row, err := ledger.InsertPending(e.ctx, corenotify.DeliveryInsert{
			NotificationID: note.ID, UserID: user.ID,
			Channel: channel, NextRetryAt: &now,
		})
		if err != nil {
			t.Fatalf("seed row: %v", err)
		}
		return row.ID
	}

	dayStart := epochTime(t, e.scalarString(
		`SELECT extract(epoch from date_trunc('day', now()))::text`))
	dayEnd := dayStart.AddDate(0, 0, 1)
	count := func() int {
		t.Helper()
		got, err := ledger.CountSentOnDay(e.ctx, corenotify.ChannelWebhook, dayStart, dayEnd)
		if err != nil {
			t.Fatalf("CountSentOnDay: %v", err)
		}
		return got
	}
	if got := count(); got != 0 {
		t.Fatalf("初始计数 = %d, want 0", got)
	}
	// sent 行：真发成功才占额度。
	sent := seed("wh-budget-sent", corenotify.ChannelWebhook)
	if err := ledger.MarkSent(e.ctx, sent); err != nil {
		t.Fatalf("mark sent: %v", err)
	}
	if got := count(); got != 1 {
		t.Fatalf("sent 后计数 = %d, want 1", got)
	}
	// failed 与 skipped 行都不计数。
	failed := seed("wh-budget-failed", corenotify.ChannelWebhook)
	if err := ledger.MarkFailed(e.ctx, failed, "refused", nil); err != nil {
		t.Fatalf("mark failed: %v", err)
	}
	skipped := seed("wh-budget-skipped", corenotify.ChannelWebhook)
	if err := ledger.MarkSkipped(e.ctx, skipped, "cooldown_hit"); err != nil {
		t.Fatalf("mark skipped: %v", err)
	}
	if got := count(); got != 1 {
		t.Fatalf("failed/skipped 后计数 = %d, want 1", got)
	}
	// email 渠道单独计数，互不混账。
	emailRow := seed("wh-budget-email", corenotify.ChannelEmail)
	if err := ledger.MarkSent(e.ctx, emailRow); err != nil {
		t.Fatalf("mark email sent: %v", err)
	}
	if got := count(); got != 1 {
		t.Fatalf("email sent 后 webhook 计数 = %d, want 1", got)
	}
	// 跨日归零：把 sent 行的 updated_at 拨回昨日，今日窗口归零。
	if _, err := e.db.Exec(e.ctx,
		`UPDATE notification_deliveries SET updated_at = $2 WHERE id = $1::uuid`,
		sent, dayStart.Add(-time.Hour)); err != nil {
		t.Fatalf("backdate: %v", err)
	}
	if got := count(); got != 0 {
		t.Fatalf("跨日后今日计数 = %d, want 0", got)
	}
	// 非法参数：渠道词表外与空窗口都拒绝。
	if _, err := ledger.CountSentOnDay(e.ctx, corenotify.ChannelInApp, dayStart, dayEnd); err == nil {
		t.Fatal("in_app 渠道不应可计数")
	}
	if _, err := ledger.CountSentOnDay(e.ctx, corenotify.ChannelWebhook, dayEnd, dayStart); err == nil {
		t.Fatal("空窗口不应可计数")
	}
}

// epochTime 把 pg 返回的 epoch 秒文本转成 UTC 时刻。
func epochTime(t *testing.T, raw string) time.Time {
	t.Helper()
	seconds, err := strconv.ParseFloat(strings.TrimSpace(raw), 64)
	if err != nil {
		t.Fatalf("parse epoch %q: %v", raw, err)
	}
	return time.Unix(int64(seconds), 0).UTC()
}
