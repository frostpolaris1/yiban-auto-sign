package notify

// 本文件实现每日签到失败摘要的生成：按业务日读失败明细，按账号聚合，
// 再按收件人 scope 生成站内信与邮件账本。
//
// # 归属
// app 层用例。端口 DigestSource、DigestAudience 由 adapters/pg 实现；
// 邮件端口复用 core/mail.Sender；本文件不写 SQL、不发 HTTP。
//
// # 口径
// 摘要独立于 Dispatch：Dispatch 只收管理员三类事件，且显式拒绝
// sign_failed_daily；摘要正文是动态聚合，不走事件文案路径。
// 空日不发：全局 failed=0 直接返回，不写行、不发信。
// 每个收件人只按自己的 scope 过滤：master 全量，admin 仅绑定校；
// scope 内 0 条时该收件人不写行。
// 订阅只看 notification_prefs(sign_failed_daily,*)；缺行按默认表，
// **不看** users.mail_notify（两条订阅面分别判定，永不 OR）。
// in_app 与 email 都关时整条跳过；email 开启时通知行与账本行同事务写入，
// 复用 B 的 insertWithDelivery（in_app 关闭时通知行仍是账本锚）。
// 去重键 sign_failed_daily/{day}：同一收件人同一业务日至多一行通知、
// 一条账本、一封邮件。
// 正文按账号聚合：掩码手机号、结论词（空串为 unknown）、尝试次数；
// 封顶 50 行 +「另有 N 条未列出」。全文过 mask.MaskText；
// 摘要正文上限 4000 字符，不复用 B 的 500 字事件正文上限。

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
)

const (
	// digestBodyChars 是摘要正文的字符上限。
	// 摘要按行聚合，50 行约 1500 字；上限远大于事件正文的 500 字。
	digestBodyChars = 4000
	// digestMaxAccounts 是一封摘要列出的账号行上限。
	digestMaxAccounts = 50
	// digestUnknownResult 是空结论词的展示值。
	digestUnknownResult = "unknown"
)

// DigestSource 读取某业务日 state='failed' 的签到任务明细。
// 实现是 pg.NotificationDigestRepo；只返回未软删账号的行。
type DigestSource interface {
	FailedAccounts(ctx context.Context, day string) ([]corenotify.FailedTask, error)
}

// DigestAudience 返回管理员摘要收件人（master 全量、admin 带绑定校）。
// 实现是 pg.NotificationDigestRepo；缺行订阅由用例按默认表解释。
type DigestAudience interface {
	DigestSubscribers(ctx context.Context) ([]corenotify.DigestSubscriber, error)
}

// digestAccount 是按账号聚合后的一行摘要。
type digestAccount struct {
	AccountID string
	SchoolID  string
	Phone     string
	Result    string
	Attempts  int
	UpdatedAt time.Time
}

// DispatchDigest 给管理员订阅者发某业务日的签到失败摘要。
//
// 每收件人一行站内信；email 订阅启用时同事务再入一条账本行。
// 重复触发由 (user_id, dedupe_key) 与账本唯一键吸收，不重复投递。
// 单个收件人失败不阻断其余收件人；返回第一条错误供调用方记日志。
func (d *Dispatcher) DispatchDigest(ctx context.Context, day string) error {
	if day == "" {
		return errs.New(errs.CodeValidation, "digest requires a business day")
	}
	if _, err := time.Parse("2006-01-02", day); err != nil {
		return errs.New(errs.CodeValidation, "digest business day must be YYYY-MM-DD")
	}
	if d.digestSource == nil || d.digestAudience == nil {
		return errs.New(errs.CodeInternal, "digest ports are not configured")
	}
	failures, err := d.digestSource.FailedAccounts(ctx, day)
	if err != nil {
		return err
	}
	accounts := aggregateFailedAccounts(failures)
	if len(accounts) == 0 {
		// 空日不发：不查收件人、不写行、不发信。
		return nil
	}
	recipients, err := d.digestAudience.DigestSubscribers(ctx)
	if err != nil {
		return err
	}
	now := d.clock.Now()
	dedupeKey := "sign_failed_daily/" + day
	var firstErr error
	skipped := 0
	for _, recipient := range recipients {
		scoped := scopeDigestAccounts(accounts, recipient)
		if len(scoped) == 0 {
			continue
		}
		inApp := enabled(recipient.Overrides, corenotify.Pair{
			Category: corenotify.CategorySignFailedDaily, Channel: corenotify.ChannelInApp,
		})
		email := enabled(recipient.Overrides, corenotify.Pair{
			Category: corenotify.CategorySignFailedDaily, Channel: corenotify.ChannelEmail,
		})
		if !inApp && !email {
			continue
		}
		title, body := digestCopy(day, scoped)
		insert := corenotify.NotificationInsert{
			UserID:    recipient.UserID,
			Category:  corenotify.CategorySignFailedDaily,
			DedupeKey: dedupeKey,
			Title:     title,
			Body:      body,
			CreatedAt: now,
		}
		if !email {
			if _, err := d.notes.Insert(ctx, insert); err != nil {
				recordError(&firstErr, err)
			}
			continue
		}
		// email 开启时通知与账本同事务写入。in_app 关闭时这一行仍是
		// 事件档案/账本锚，会出现在管理员站内信列表（同 B 的实施单裁决）。
		wasSkipped, err := d.insertWithDelivery(ctx, insert, now)
		if err != nil {
			recordError(&firstErr, err)
			continue
		}
		if wasSkipped {
			skipped++
		}
	}
	d.warnIfMailDisabled(skipped)
	return firstErr
}

// aggregateFailedAccounts 按账号收敛失败明细。
// 同账号多任务取 updated_at 最新的 result 与最大 attempts；
// 输出按账号标识升序，保证封顶裁切与正文顺序稳定。
func aggregateFailedAccounts(failures []corenotify.FailedTask) []digestAccount {
	byAccount := make(map[string]*digestAccount, len(failures))
	for _, failure := range failures {
		if failure.AccountID == "" {
			continue
		}
		current, ok := byAccount[failure.AccountID]
		if !ok {
			byAccount[failure.AccountID] = &digestAccount{
				AccountID: failure.AccountID,
				SchoolID:  failure.SchoolID,
				Phone:     failure.Phone,
				Result:    failure.Result,
				Attempts:  failure.Attempts,
				UpdatedAt: failure.UpdatedAt,
			}
			continue
		}
		if failure.Attempts > current.Attempts {
			current.Attempts = failure.Attempts
		}
		// 同一时刻以来源顺序的后一条为准；来源按 (account, updated_at) 排序。
		if !failure.UpdatedAt.Before(current.UpdatedAt) {
			current.Result = failure.Result
			current.Phone = failure.Phone
			current.SchoolID = failure.SchoolID
			current.UpdatedAt = failure.UpdatedAt
		}
	}
	out := make([]digestAccount, 0, len(byAccount))
	for _, account := range byAccount {
		out = append(out, *account)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].AccountID < out[j].AccountID })
	return out
}

// scopeDigestAccounts 按收件人 scope 过滤：master 全量，admin 仅绑定校。
// admin 无绑定校时返回空集合（空集合不等于全量）。
func scopeDigestAccounts(accounts []digestAccount, recipient corenotify.DigestSubscriber) []digestAccount {
	if recipient.Master {
		return accounts
	}
	if len(recipient.Schools) == 0 {
		return nil
	}
	bound := make(map[string]bool, len(recipient.Schools))
	for _, schoolID := range recipient.Schools {
		bound[schoolID] = true
	}
	out := make([]digestAccount, 0, len(accounts))
	for _, account := range accounts {
		if bound[account.SchoolID] {
			out = append(out, account)
		}
	}
	return out
}

// digestCopy 生成摘要的标题与正文。标题「{day} 签到失败摘要」，
// 正文首行给总数，每账号一行；超过上限追加未列出条数。
// 标题与正文都过 mask.MaskText，正文按字符截断到 digestBodyChars。
func digestCopy(day string, accounts []digestAccount) (string, string) {
	lines := make([]string, 0, digestMaxAccounts+2)
	lines = append(lines, fmt.Sprintf("%s 共有 %d 个账号签到失败。", day, len(accounts)))
	limit := len(accounts)
	if limit > digestMaxAccounts {
		limit = digestMaxAccounts
	}
	for _, account := range accounts[:limit] {
		result := account.Result
		if result == "" {
			result = digestUnknownResult
		}
		lines = append(lines, fmt.Sprintf("%s：%s（尝试 %d 次）",
			mask.MaskPhone(account.Phone), result, account.Attempts))
	}
	if len(accounts) > digestMaxAccounts {
		lines = append(lines, fmt.Sprintf("另有 %d 条未列出。", len(accounts)-digestMaxAccounts))
	}
	title := fmt.Sprintf("%s 签到失败摘要", day)
	return truncateChars(mask.MaskText(title), maxTitleChars),
		truncateChars(mask.MaskText(strings.Join(lines, "\n")), digestBodyChars)
}
