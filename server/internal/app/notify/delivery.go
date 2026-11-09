package notify

// 本文件实现通知投递：管理员事件 → 站内信 + 邮件 + webhook 推送；
// 用户本人失败邮件；邮件与 webhook 账本的重试循环。
//
// # 归属
// app 层用例。端口 NotificationWriter、DeliveryLedger 与 Audience 由
// adapters/pg 实现；邮件端口是 core/mail.Sender，webhook 端口是
// core/webhook.Sender；本文件不写 SQL、不发 HTTP。
//
// # 口径
// 站内信行同时是事件档案与投递账本锚：收件人只要有一个渠道启用就写一行，
// email 与 webhook 行通过 notification_id 外键挂靠。所有开关都关才整条跳过。
// **in_app 关闭、其他渠道开启时锚点行仍会出现在管理员站内信列表**：
// 这是实施单裁决（账本外键需要父行），本批不为可见性再拆表或删行。
// 用户失败邮件只看 users.mail_notify；管理员事件只看 notification_prefs。
// 两条订阅面分别判定，**永不 OR**。
// 文案在本文件拼装：手机号在落库前过 mask，title ≤ 80 字、body ≤ 500 字。
// 通知与账本行在同一事务写入（NotificationWriter.InsertWithPending）：
// 中途崩溃不会留下没有账本的邮件事件。email 与 webhook 各一次
// InsertWithPending：通知行的去重键保证同一事件只落一行站内信，
// 两个渠道的账本行各自独立入同一事务路径。
// 投递是至少一次：站内信与账本各一道唯一键吸收重复事件。
// 重试间隔 1m/5m/30m，共 3 次重试后落 failed；不可重试与 panic 立即 failed。
// 退避窗口最长约 36 分钟；投递时**不复核** mail_notify/订阅开关，
// 代价是关掉开关前已入队的投递仍会送达。
// 投递时复核收件人仍是 active 且未软删：不活跃则 skipped(recipient_inactive)，
// 不留永久 pending 行。
// SMTP 未配置时仍写通知行与账本行，账本立即 skipped(smtp_not_configured)；
// webhook 端口未配置同口径，落 skipped(webhook_not_configured)：
// 跳过是显性事实，不静默丢弃。
// webhook 发送期两道判定（调端口之前）：同题节流（process-local map，
// 重启清零）与当日额度（账本本身即单源：count(sent) 达上限即跳过，
// 失败天然不计数、无需退还）。额度按业务日窗口计数，时区由装配注入，
// 与摘要的业务日 helper 同源（coreclock.BusinessDay）。
// 多管理员启用 webhook 时账本按订阅者逐行记账：每事件每订阅者一行，
// 发送期同题节流保证全局至多一条真发，其余 skipped(cooldown_hit)——
// 账本是诚实簿记。
// Dispatch 只服务管理员的三类事件；sign_failed_daily 的管理员摘要走
// DispatchDigest（digest.go 的独立入口），**不复用** Dispatch。

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/logfield"
	coremail "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mail"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
	corewebhook "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/webhook"
)

const (
	// DeliveryInterval 是重试循环的扫描间隔。
	// 首封邮件最迟在一个间隔内发出；退避档位按分钟计，间隔不必更细。
	DeliveryInterval = 30 * time.Second
	// deliveryBatch 是单轮扫描的到期行上限。
	deliveryBatch = 50
	// deliveryTimeout 是一次 SMTP 投递的总预算，与 adapters/mail.DefaultTimeout 同值。
	deliveryTimeout = 10 * time.Second
	// ledgerTimeout 是投递后写账本的上限。
	// 停机时主 ctx 已取消，但账本仍要落：写不进去会留下 pending 行，重启后重复投递。
	ledgerTimeout = 2 * time.Second
	// deliveryStopTimeout 是 Stop 等待在途单轮的硬上限。
	deliveryStopTimeout = 5 * time.Second

	// maxTitleChars 与契约的 title 上限一致。
	maxTitleChars = 80
	// maxBodyChars 与契约的 body 上限一致。
	maxBodyChars = 500
	// maxReasonChars 是失败原因进入正文前的截断长度。
	maxReasonChars = 200

	// skipReasonSMTPNotConfigured 是 SMTP 未配置的跳过原因。
	skipReasonSMTPNotConfigured = "smtp_not_configured"
	// skipReasonWebhookNotConfigured 是 webhook 端口未配置的跳过原因。
	skipReasonWebhookNotConfigured = "webhook_not_configured"
	// skipReasonRecipientInactive 是收件人已停用或软删的跳过原因。
	skipReasonRecipientInactive = "recipient_inactive"
	// skipReasonCooldownHit 是 webhook 同题节流命中的跳过原因。
	skipReasonCooldownHit = "cooldown_hit"
	// skipReasonBudgetExhausted 是 webhook 当日额度用尽的跳过原因。
	skipReasonBudgetExhausted = "budget_exhausted"

	// mailSubjectPrefix 是通知邮件的主题前缀。
	mailSubjectPrefix = "【易班签到】"
	// mailFooter 是通知邮件的固定尾注。
	mailFooter = "此邮件由系统自动发送，请勿回复。"
)

// deliveryRetryDelays 是第 n 次失败后的退避（n 从 0 起）：1m/5m/30m。
// 长度即「共 3 次重试」；第 4 次失败落 failed。
var deliveryRetryDelays = []time.Duration{time.Minute, 5 * time.Minute, 30 * time.Minute}

// NotificationWriter 写站内信行。实现是 pg.NotificationRepo。
type NotificationWriter interface {
	Insert(ctx context.Context, in corenotify.NotificationInsert) (corenotify.Notification, error)
	// InsertWithPending 在同一事务里写站内信与待投递账本行。
	// pending 的 NotificationID/UserID 由实现按 note 填入，调用方留空；
	// Channel 与 NextRetryAt 以 pending 为准。重复事件返回已存在的两行。
	InsertWithPending(ctx context.Context, note corenotify.NotificationInsert,
		pending corenotify.DeliveryInsert) (corenotify.NotificationWithDelivery, error)
}

// DeliveryLedger 读写投递账本。实现是 pg.NotificationDeliveryRepo。
type DeliveryLedger interface {
	InsertPending(ctx context.Context, in corenotify.DeliveryInsert) (corenotify.Delivery, error)
	// ListDueForSend 返回 next_retry_at <= now 的 pending 行，带通知正文与收件地址。
	ListDueForSend(ctx context.Context, now time.Time, limit int) ([]corenotify.DueDelivery, error)
	MarkSent(ctx context.Context, id string) error
	MarkFailed(ctx context.Context, id, errorKind string, nextRetryAt *time.Time) error
	MarkSkipped(ctx context.Context, id, reason string) error
	// CountSentOnDay 统计某渠道在 [from, to) 时间窗内真发成功的行数。
	// webhook 当日额度的单源就是账本本身：只有 sent 行占用，
	// failed/skipped 不计数、无需退还。
	CountSentOnDay(ctx context.Context, channel corenotify.Channel, from, to time.Time) (int, error)
}

// Audience 是收件人解析端口。
//
// AdminSubscribers 返回某校事件的管理员收件人：master 收全部，
// admin 只收 admin_schools 绑定校；只取 active 且未软删的用户。
// AccountPhone 供文案掩码取号；FailureOwner 只回 active、未软删的属主。
// RecipientActive 供投递前复核：入队后收件人可能被停用或软删。
type Audience interface {
	AdminSubscribers(ctx context.Context, schoolID string) ([]corenotify.Subscriber, error)
	AccountPhone(ctx context.Context, accountID string) (string, error)
	FailureOwner(ctx context.Context, accountID string) (corenotify.Owner, error)
	RecipientActive(ctx context.Context, userID string) (bool, error)
}

// DispatcherDeps 是投递服务的依赖。
// Mail 与 Webhook 可为 nil：对应渠道的账本行分别落
// skipped(smtp_not_configured) 与 skipped(webhook_not_configured)，
// 其余行为不变。
// WebhookCooldown 与 WebhookDailyMax 是 webhook 发送期两道判定的参数：
// 冷却 <=0 表示不节流，日额度 <=0 表示不限额。
// Location 是业务日窗口的时区（额度计数与摘要的业务日同源）；
// nil 按 UTC。
// DigestSource 与 DigestAudience 是每日摘要的可选端口：缺失时
// DispatchDigest 报错，三类事件与用户失败邮件的投递不受影响。
type DispatcherDeps struct {
	Notes           NotificationWriter
	Ledger          DeliveryLedger
	Audience        Audience
	Mail            coremail.Sender
	Webhook         corewebhook.Sender
	WebhookCooldown time.Duration
	WebhookDailyMax int
	Location        *time.Location
	Clock           coreclock.Clock
	Logger          *slog.Logger
	DigestSource    DigestSource
	DigestAudience  DigestAudience
}

// Dispatcher 是通知投递用例服务。
type Dispatcher struct {
	notes           NotificationWriter
	ledger          DeliveryLedger
	audience        Audience
	mail            coremail.Sender
	webhook         corewebhook.Sender
	webhookCooldown time.Duration
	webhookDailyMax int
	location        *time.Location
	clock           coreclock.Clock
	log             *slog.Logger
	digestSource    DigestSource
	digestAudience  DigestAudience
	webhookThrottle webhookThrottle
}

// webhookThrottle 是 webhook 同题节流的 process-local 状态：
// 重启清零（与测试邮件冷却的口径一致），键是归一化后的推送 title。
type webhookThrottle struct {
	mu   sync.Mutex
	sent map[string]time.Time
}

// allow 报告窗口内是否已有同题推送。未命中时记录本次占位并放行；
// cooldown <=0 表示节流关闭，不占位（也就无需失败释放）。
func (t *webhookThrottle) allow(title string, cooldown time.Duration, now time.Time) bool {
	if cooldown <= 0 {
		return true
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.sent == nil {
		t.sent = map[string]time.Time{}
	}
	if last, ok := t.sent[title]; ok && now.Sub(last) < cooldown {
		return false
	}
	t.sent[title] = now
	return true
}

// release 回滚本次占位：发送失败后释放，重试行才不会在窗口内被自己静音。
// 投递单轮串行（单进程不变量），占位只有一个写入者，直接删除即可。
func (t *webhookThrottle) release(title string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	delete(t.sent, title)
}

// NewDispatcher 构造投递服务。缺站内信写入、账本、收件人端口或时钟即失败。
func NewDispatcher(deps DispatcherDeps) (*Dispatcher, error) {
	if deps.Notes == nil {
		return nil, errors.New("notify: nil notification writer")
	}
	if deps.Ledger == nil {
		return nil, errors.New("notify: nil delivery ledger")
	}
	if deps.Audience == nil {
		return nil, errors.New("notify: nil audience")
	}
	if deps.Clock == nil {
		return nil, errors.New("notify: nil clock")
	}
	log := deps.Logger
	if log == nil {
		log = slog.Default()
	}
	location := deps.Location
	if location == nil {
		location = time.UTC
	}
	return &Dispatcher{
		notes:           deps.Notes,
		ledger:          deps.Ledger,
		audience:        deps.Audience,
		mail:            deps.Mail,
		webhook:         deps.Webhook,
		webhookCooldown: deps.WebhookCooldown,
		webhookDailyMax: deps.WebhookDailyMax,
		location:        location,
		clock:           deps.Clock,
		log:             log,
		digestSource:    deps.DigestSource,
		digestAudience:  deps.DigestAudience,
	}, nil
}

// Dispatch 把一条管理员事件投给该校的订阅者。
//
// 每收件人一行站内信；email 与 webhook 订阅启用时各自在同一事务路径里
// 再入一条账本行。重复事件由 (user_id, dedupe_key) 与账本唯一键吸收，
// 不重复投递。单个收件人失败不阻断其余收件人；返回第一条错误供调用方
// 记日志。只接受管理员的三类事件：sign_failed_daily 的摘要走
// DispatchDigest 的独立入口，不经本方法。
func (d *Dispatcher) Dispatch(ctx context.Context, event corenotify.Event) error {
	if !event.Category.Valid() || event.Category == corenotify.CategorySignFailedDaily {
		return errs.New(errs.CodeValidation, "Dispatch only accepts administrator event categories")
	}
	if event.DedupeKey == "" {
		return errs.New(errs.CodeValidation, "dedupe key is required")
	}
	title, body, err := d.adminCopy(ctx, event)
	if err != nil {
		return err
	}
	title, body = sanitizeCopy(title, body)
	recipients, err := d.audience.AdminSubscribers(ctx, event.SchoolID)
	if err != nil {
		return err
	}
	now := d.clock.Now()
	var firstErr error
	skipped := 0
	for _, recipient := range recipients {
		inApp := enabled(recipient.Overrides, corenotify.Pair{
			Category: event.Category, Channel: corenotify.ChannelInApp,
		})
		email := enabled(recipient.Overrides, corenotify.Pair{
			Category: event.Category, Channel: corenotify.ChannelEmail,
		})
		webhook := enabled(recipient.Overrides, corenotify.Pair{
			Category: event.Category, Channel: corenotify.ChannelWebhook,
		})
		if !inApp && !email && !webhook {
			continue
		}
		insert := corenotify.NotificationInsert{
			UserID:    recipient.UserID,
			Category:  event.Category,
			DedupeKey: event.DedupeKey,
			Title:     title,
			Body:      body,
			SchoolID:  event.SchoolID,
			CreatedAt: now,
		}
		if !email && !webhook {
			if _, err := d.notes.Insert(ctx, insert); err != nil {
				recordError(&firstErr, err)
			}
			continue
		}
		if email {
			// email 开启时通知与账本同事务写入。in_app 关闭时这一行仍是
			// 事件档案/账本锚，会出现在管理员站内信列表（实施单裁决）。
			wasSkipped, err := d.insertWithChannel(ctx, insert, now, corenotify.ChannelEmail)
			if err != nil {
				recordError(&firstErr, err)
				continue
			}
			if wasSkipped {
				skipped++
			}
		}
		if webhook {
			// webhook 账本行与 email 行同构、按订阅者逐行记账：
			// 通知行的去重键命中后返回同一站内信行，webhook 账本行
			// 经同一条事务路径挂靠（见 insertWithChannel）。
			wasSkipped, err := d.insertWithChannel(ctx, insert, now, corenotify.ChannelWebhook)
			if err != nil {
				recordError(&firstErr, err)
				continue
			}
			if wasSkipped {
				skipped++
			}
		}
	}
	d.warnIfDisabled(skipped)
	return firstErr
}

// DispatchUserFailure 把一条签到失败发给账号属主本人。
//
// 只写一行站内信作为账本锚（普通用户没有站内信读端点，行不外显），
// 再入一条 email 账本行。开关只看 users.mail_notify；属主不活跃即跳过。
// 去重键是 user_fail_mail/{account_id}/{day}：每账号每业务日至多一封。
func (d *Dispatcher) DispatchUserFailure(ctx context.Context, failure corenotify.UserFailure) error {
	if failure.AccountID == "" || failure.Day == "" {
		return errs.New(errs.CodeValidation, "user failure requires an account and a day")
	}
	owner, err := d.audience.FailureOwner(ctx, failure.AccountID)
	if err != nil {
		// 账号或属主已不可用：没有可投的收件人，不是投递失败。
		if errs.From(err).Code == errs.CodeNotFound {
			return nil
		}
		return err
	}
	if !owner.MailNotify {
		return nil
	}
	masked, err := d.maskedPhone(ctx, failure.AccountID)
	if err != nil {
		return err
	}
	title := fmt.Sprintf("账号 %s 签到失败", masked)
	body := fmt.Sprintf("账号 %s 在 %s 的签到失败（%s）。", masked, failure.Day, failure.Outcome)
	if failure.Reason != "" {
		body += "原因：" + truncateChars(mask.MaskText(failure.Reason), maxReasonChars)
	}
	title, body = sanitizeCopy(title, body)
	now := d.clock.Now()
	// 通知行与账本行同事务写入：用户没有站内信读端点，
	// 这一行只作账本锚（行不外显）。用户失败邮件只走 email 渠道：
	// webhook 的订阅面是 notification_prefs，永不与 mail_notify OR。
	skipped, err := d.insertWithChannel(ctx, corenotify.NotificationInsert{
		UserID:    owner.UserID,
		Category:  corenotify.CategorySignFailedDaily,
		DedupeKey: "user_fail_mail/" + failure.AccountID + "/" + failure.Day,
		Title:     title,
		Body:      body,
		SchoolID:  failure.SchoolID,
		CreatedAt: now,
	}, now, corenotify.ChannelEmail)
	if err != nil {
		return err
	}
	if skipped {
		d.warnIfDisabled(1)
	}
	return nil
}

// SendDue 跑一轮到期投递，返回第一条账本错误。
// 单轮串行：单进程不变量，不需要租约或 SKIP LOCKED。
func (d *Dispatcher) SendDue(ctx context.Context) error {
	rows, err := d.ledger.ListDueForSend(ctx, d.clock.Now(), deliveryBatch)
	if err != nil {
		return err
	}
	var firstErr error
	for _, row := range rows {
		if err := d.deliver(ctx, row); err != nil {
			recordError(&firstErr, err)
		}
	}
	return firstErr
}

// deliver 投递一条到期行并记终态。
//
// 发送前复核收件人仍是 active 且未软删：入队后可能被停用或软删，
// 这类行落 skipped(recipient_inactive)，不留永久 pending。
// 按渠道分流：email 行走邮件端口，webhook 行走 webhook 端口。
// 失败分类按渠道取词表：email 行用 core/mail.Kind，webhook 行用
// core/webhook.Kind；KindPanic 由 send 的 recover 产出。
// 账本写入用 WithoutCancel：停机取消不得留下 pending 行造成重复投递。
func (d *Dispatcher) deliver(ctx context.Context, row corenotify.DueDelivery) error {
	active, err := d.audience.RecipientActive(ctx, row.UserID)
	if err != nil {
		// 读库失败不等于收件人失效：保留 pending，下一轮再试。
		d.log.Warn("notify: recipient check failed",
			logfield.KeyEvent, "notify.recipient_check_failed",
			logfield.KeyUserID, row.UserID,
			logfield.KeyError, err.Error())
		return err
	}
	if !active {
		return d.skipInactive(ctx, row)
	}
	if row.Channel == corenotify.ChannelWebhook {
		return d.deliverWebhook(ctx, row)
	}
	if d.mail == nil {
		return d.markSkipped(ctx, row.ID, skipReasonSMTPNotConfigured)
	}
	kind, retryable, err := d.send(ctx, row)
	writeCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), ledgerTimeout)
	defer cancel()
	if err == nil {
		if markErr := d.ledger.MarkSent(writeCtx, row.ID); markErr != nil {
			d.log.Warn("notify: record delivery result failed",
				logfield.KeyEvent, "notify.delivery_result_failed",
				logfield.KeyError, markErr.Error())
			return markErr
		}
		d.log.Info("notify: mail delivered",
			logfield.KeyEvent, "notify.delivered",
			logfield.KeyEmail, mask.EmailLabel(row.Email))
		return nil
	}
	attrs := []any{
		logfield.KeyEvent, "notify.delivery_failed",
		logfield.KeyKind, string(kind),
		logfield.KeyRetryable, retryable,
		logfield.KeyEmail, mask.EmailLabel(row.Email),
	}
	next := d.nextRetry(row.Attempts, retryable)
	if markErr := d.ledger.MarkFailed(writeCtx, row.ID, string(kind), next); markErr != nil {
		attrs = append(attrs, logfield.KeyError, markErr.Error())
		d.log.Warn("notify: mail delivery failed", attrs...)
		return markErr
	}
	d.log.Warn("notify: mail delivery failed", attrs...)
	return nil
}

// deliverWebhook 投递一条 webhook 到期行。
//
// 两道发送期判定都排在调端口之前：
// ① 同题节流：键是归一化后的推送 title，process-local map，重启清零；
// ② 当日额度：账本 count(sent, webhook, 业务日窗口) 达上限即跳过，
//
//	额度单源在账本本身（只有真发成功才占用，失败天然无需退还）。
//
// 节流命中与额度用尽都落 skipped，不是发送尝试（attempts 不递增）。
// 发送失败释放节流占位：失败行会重试，重试不该在窗口内被自己静音
// （与旧线「失败释放」同口径）。日志不含 URL 与 SendKey。
func (d *Dispatcher) deliverWebhook(ctx context.Context, row corenotify.DueDelivery) error {
	if d.webhook == nil {
		return d.markSkipped(ctx, row.ID, skipReasonWebhookNotConfigured)
	}
	now := d.clock.Now()
	title := normalizeWebhookTitle(row.Title)
	if !d.webhookThrottle.allow(title, d.webhookCooldown, now) {
		return d.markSkipped(ctx, row.ID, skipReasonCooldownHit)
	}
	if d.webhookDailyMax > 0 {
		from, to := businessDayWindow(now, d.location)
		used, err := d.ledger.CountSentOnDay(ctx, corenotify.ChannelWebhook, from, to)
		if err != nil {
			// 读库失败不是额度判定结论：释放占位、保留 pending 下一轮再试。
			d.webhookThrottle.release(title)
			d.log.Warn("notify: webhook budget check failed",
				logfield.KeyEvent, "notify.webhook_budget_check_failed",
				logfield.KeyError, err.Error())
			return err
		}
		if used >= d.webhookDailyMax {
			d.webhookThrottle.release(title)
			return d.markSkipped(ctx, row.ID, skipReasonBudgetExhausted)
		}
	}
	kind, retryable, err := d.sendWebhook(ctx, row)
	writeCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), ledgerTimeout)
	defer cancel()
	if err == nil {
		if markErr := d.ledger.MarkSent(writeCtx, row.ID); markErr != nil {
			d.log.Warn("notify: record delivery result failed",
				logfield.KeyEvent, "notify.delivery_result_failed",
				logfield.KeyError, markErr.Error())
			return markErr
		}
		d.log.Info("notify: webhook delivered",
			logfield.KeyEvent, "notify.delivered",
			logfield.KeyKind, string(corenotify.ChannelWebhook))
		return nil
	}
	// 失败释放节流占位：额度单源在账本，无需退还；节流必须释放，
	// 否则一次对端瞬断会把同题告警静音一个窗口。
	d.webhookThrottle.release(title)
	attrs := []any{
		logfield.KeyEvent, "notify.delivery_failed",
		logfield.KeyKind, string(kind),
		logfield.KeyRetryable, retryable,
		logfield.KeyUserID, row.UserID,
	}
	next := d.nextRetry(row.Attempts, retryable)
	if markErr := d.ledger.MarkFailed(writeCtx, row.ID, string(kind), next); markErr != nil {
		attrs = append(attrs, logfield.KeyError, markErr.Error())
		d.log.Warn("notify: webhook delivery failed", attrs...)
		return markErr
	}
	d.log.Warn("notify: webhook delivery failed", attrs...)
	return nil
}

// sendWebhook 调一次 webhook 端口。panic 按 KindPanic 记账，panic 值不进日志。
func (d *Dispatcher) sendWebhook(ctx context.Context, row corenotify.DueDelivery) (kind corewebhook.Kind, retryable bool, err error) {
	defer func() {
		if r := recover(); r != nil {
			kind, retryable, err = corewebhook.KindPanic, false, errors.New("notify: webhook sender panicked")
		}
	}()
	sendCtx, cancel := context.WithTimeout(ctx, deliveryTimeout)
	defer cancel()
	if sendErr := d.webhook.Send(sendCtx, corewebhook.Push{
		Title: row.Title, Content: row.Body,
	}); sendErr != nil {
		kind, retryable = corewebhook.Classify(sendErr)
		return kind, retryable, sendErr
	}
	return "", false, nil
}
func (d *Dispatcher) send(ctx context.Context, row corenotify.DueDelivery) (kind coremail.Kind, retryable bool, err error) {
	defer func() {
		if r := recover(); r != nil {
			kind, retryable, err = coremail.KindPanic, false, errors.New("notify: mail sender panicked")
		}
	}()
	sendCtx, cancel := context.WithTimeout(ctx, deliveryTimeout)
	defer cancel()
	if sendErr := d.mail.Send(sendCtx, coremail.Message{
		To:      row.Email,
		Subject: mailSubjectPrefix + row.Title,
		Text:    row.Body + "\n\n" + mailFooter + "\n",
	}); sendErr != nil {
		kind, retryable = coremail.Classify(sendErr)
		return kind, retryable, sendErr
	}
	return "", false, nil
}

// adminCopy 返回事件的 title 与 body。文案单源在本文件，业务点不拼文案。
func (d *Dispatcher) adminCopy(ctx context.Context, event corenotify.Event) (string, string, error) {
	switch event.Category {
	case corenotify.CategoryCredentialInvalid:
		masked, err := d.maskedPhone(ctx, event.AccountID)
		if err != nil {
			return "", "", err
		}
		return "账号凭据失效",
			fmt.Sprintf("账号 %s 在 %s 的签到中凭据失效，已移出排期。", masked, event.Day), nil
	case corenotify.CategoryReviewPending:
		masked, err := d.maskedPhone(ctx, event.AccountID)
		if err != nil {
			return "", "", err
		}
		if event.AutoApproved {
			return "账号已自动通过审核", fmt.Sprintf("账号 %s 已自动通过审核。", masked), nil
		}
		return "账号待审核", fmt.Sprintf("账号 %s 已提交审核，请及时处理。", masked), nil
	case corenotify.CategoryCapacityRejected:
		body := fmt.Sprintf("%s 的计划需求 %d、容量 %d，缺口 %d。",
			event.Day, event.Capacity.Demand, event.Capacity.Capacity, event.Capacity.Shortfall)
		if event.Capacity.Binding != "" {
			body += "瓶颈：" + event.Capacity.Binding + "。"
		}
		if event.Capacity.SuggestedWorkers > 0 {
			body += fmt.Sprintf("建议 YIBAN_WORKERS=%d。", event.Capacity.SuggestedWorkers)
		}
		return "容量不足", body, nil
	default:
		return "", "", errs.New(errs.CodeValidation, "unsupported notification category")
	}
}

// maskedPhone 取账号手机号的掩码标签。完整号码不出本函数。
func (d *Dispatcher) maskedPhone(ctx context.Context, accountID string) (string, error) {
	if accountID == "" {
		return "", errs.New(errs.CodeValidation, "notification requires an account")
	}
	phone, err := d.audience.AccountPhone(ctx, accountID)
	if err != nil {
		return "", err
	}
	return mask.MaskPhone(phone), nil
}

// insertWithChannel 在同一事务里写通知与指定渠道的账本行。
// 同一事件第二个渠道再入时，通知行的 (user_id, dedupe_key) 去重键
// 命中已存在行，新渠道的账本行照常经同一条事务路径挂靠。
// 端口未配置（email 无 SMTP、webhook 无出口）时账本立即落 skipped：
// 跳过是显性事实，不留给重试循环空转。
// 返回是否发生了跳过（调用方按每条事件聚合成一条警告）。
func (d *Dispatcher) insertWithChannel(ctx context.Context, insert corenotify.NotificationInsert,
	now time.Time, channel corenotify.Channel) (bool, error) {
	result, err := d.notes.InsertWithPending(ctx, insert, corenotify.DeliveryInsert{
		Channel:     channel,
		NextRetryAt: &now,
	})
	if err != nil {
		return false, err
	}
	switch channel {
	case corenotify.ChannelEmail:
		if d.mail != nil {
			return false, nil
		}
		if err := d.markSkipped(ctx, result.Delivery.ID, skipReasonSMTPNotConfigured); err != nil {
			return false, err
		}
	case corenotify.ChannelWebhook:
		if d.webhook != nil {
			return false, nil
		}
		if err := d.markSkipped(ctx, result.Delivery.ID, skipReasonWebhookNotConfigured); err != nil {
			return false, err
		}
	default:
		return false, errs.New(errs.CodeValidation, "delivery channel has no ledger path")
	}
	return true, nil
}

// skipInactive 把一条到期行记为 recipient_inactive。
// 日志只记 user_id 与原因：邮箱不进日志。
func (d *Dispatcher) skipInactive(ctx context.Context, row corenotify.DueDelivery) error {
	writeCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), ledgerTimeout)
	defer cancel()
	if err := d.markSkipped(writeCtx, row.ID, skipReasonRecipientInactive); err != nil {
		return err
	}
	d.log.Info("notify: delivery skipped for inactive recipient",
		logfield.KeyEvent, "notify.delivery_skipped",
		logfield.KeyReason, skipReasonRecipientInactive,
		logfield.KeyUserID, row.UserID)
	return nil
}

// markSkipped 把一条待投递行记为跳过，并记录失败原因。
func (d *Dispatcher) markSkipped(ctx context.Context, deliveryID, reason string) error {
	if err := d.ledger.MarkSkipped(ctx, deliveryID, reason); err != nil {
		d.log.Warn("notify: record skipped delivery failed",
			logfield.KeyEvent, "notify.delivery_result_failed",
			logfield.KeyReason, reason,
			logfield.KeyError, err.Error())
		return err
	}
	return nil
}

// nextRetry 返回下一次重试时刻；不可重试或重试次数用完返回 nil（落 failed）。
// attempts 是账本里已记录的发送尝试数：0 取 1m，1 取 5m，2 取 30m，3 起落 failed。
// MarkSent 与 MarkFailed 都递增 attempts，skipped 不是尝试、不递增。
func (d *Dispatcher) nextRetry(attempts int, retryable bool) *time.Time {
	if !retryable || attempts < 0 || attempts >= len(deliveryRetryDelays) {
		return nil
	}
	next := d.clock.Now().Add(deliveryRetryDelays[attempts])
	return &next
}

// warnIfDisabled 聚合一条显性警告：SMTP 或 webhook 未配置导致本轮有跳过。
// 两种原因分开成对日志计数，不混淆渠道。
func (d *Dispatcher) warnIfDisabled(skipped int) {
	if skipped == 0 {
		return
	}
	if d.mail == nil {
		d.log.Warn("notify: SMTP is not configured; email deliveries were skipped",
			logfield.KeyEvent, "notify.delivery_skipped",
			logfield.KeyReason, skipReasonSMTPNotConfigured,
			logfield.KeyCount, skipped)
	}
	if d.webhook == nil {
		d.log.Warn("notify: webhook outlet is not configured; webhook deliveries were skipped",
			logfield.KeyEvent, "notify.delivery_skipped",
			logfield.KeyReason, skipReasonWebhookNotConfigured,
			logfield.KeyCount, skipped)
	}
}

// maxWebhookTitleChars 与 Server酱服务端的 title 上限一致：
// 节流键与出口层用同一归一化口径，同题判定不因出口不同而漂移。
const maxWebhookTitleChars = 32

// normalizeWebhookTitle 把推送标题规整成节流键的形态：
// 去 CR/LF、去首尾空白、按字符截 32（与 adapters/webhook 的出口
// 归一化同口径）。落库 title 已掩码，这里只做出口形态归一。
func normalizeWebhookTitle(title string) string {
	title = strings.ReplaceAll(title, "\r", " ")
	title = strings.ReplaceAll(title, "\n", " ")
	title = strings.TrimSpace(title)
	runes := []rune(title)
	if len(runes) > maxWebhookTitleChars {
		return string(runes[:maxWebhookTitleChars])
	}
	return title
}

// businessDayWindow 返回 now 所在业务日的 [起, 止) 时刻窗口：
// 与摘要的业务日 helper 同源（coreclock.BusinessDay），
// 额度计数按「真发成功时刻所在的业务日」归日。
func businessDayWindow(now time.Time, loc *time.Location) (time.Time, time.Time) {
	day := coreclock.BusinessDay(now.In(loc), loc)
	start, err := time.ParseInLocation("2006-01-02", day, loc)
	if err != nil {
		// 词表由 BusinessDay 保证可解析；防御性回退取整日 UTC 窗口。
		return now.Truncate(24 * time.Hour), now.Truncate(24*time.Hour).AddDate(0, 0, 1)
	}
	return start, start.AddDate(0, 0, 1)
}

// enabled 返回一对订阅的有效状态：显式覆盖优先，缺行按默认表。
func enabled(overrides map[Pair]bool, pair Pair) bool {
	if value, ok := overrides[pair]; ok {
		return value
	}
	return Default(pair)
}

// sanitizeCopy 对 title/body 做掩码与截断。落库与发信共用同一份文本。
func sanitizeCopy(title, body string) (string, string) {
	return truncateChars(mask.MaskText(title), maxTitleChars),
		truncateChars(mask.MaskText(body), maxBodyChars)
}

// truncateChars 按字符截断，不按字节：多字节文本截断后必须仍是合法 UTF-8。
func truncateChars(s string, limit int) string {
	if limit <= 0 || utf8.RuneCountInString(s) <= limit {
		return s
	}
	return string([]rune(s)[:limit])
}

// recordError 保留第一条错误，不覆盖已有错误。
func recordError(dst *error, err error) {
	if err != nil && *dst == nil {
		*dst = err
	}
}

// Start 起重试循环，返回停止函数。启动即跑一轮，之后每 DeliveryInterval 一轮。
// 单轮失败只记日志：投递失败不该挡住签到或计划构建。
func Start(ctx context.Context, d *Dispatcher, logger *slog.Logger) func() {
	log := logger
	if log == nil {
		log = slog.Default()
	}
	runCtx, cancel := context.WithCancel(ctx)
	done := make(chan struct{})
	go func() {
		defer close(done)
		ticker := time.NewTicker(DeliveryInterval)
		defer ticker.Stop()
		for {
			if err := d.SendDue(runCtx); err != nil {
				log.Warn("notify: delivery round failed", logfield.KeyError, err.Error())
			}
			select {
			case <-runCtx.Done():
				return
			case <-ticker.C:
			}
		}
	}()
	var once sync.Once
	return func() {
		once.Do(func() {
			cancel()
			select {
			case <-done:
			case <-time.After(deliveryStopTimeout):
				log.Warn("notify: delivery loop did not stop in time")
			}
		})
	}
}
