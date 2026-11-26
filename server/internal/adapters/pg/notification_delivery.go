package pg

// 通知投递的收件人解析与到期行读取。
//
// 功能：管理员收件人（master 全量 + admin 绑定校 + 覆盖订阅）、
// 账号手机号（文案掩码用）、账号属主的失败邮件开关、
// 到期投递行的只读读取与认领（含通知正文与收件地址）。
// 本文件不判文案、不判默认订阅、不投递邮件、不写审计。
//
// 归属：adapters 层。app/notify 只依赖端口，不见本类型。
//
// 口径：收件人只取 active 且未软删的用户；admin 无绑定校时收不到任何
// 院校事件（空集合不等于全量）。手机号与邮箱只进写模型，
// 由用例掩码后再落库/进日志。

import (
	"context"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
)

// NotificationAudienceRepo 解析通知收件人与投递取数。
type NotificationAudienceRepo struct{ db *DB }

// NewNotificationAudienceRepo 构造收件人解析仓储。
func NewNotificationAudienceRepo(db *DB) *NotificationAudienceRepo {
	return &NotificationAudienceRepo{db: db}
}

// adminSubscribersSQL 是一份收件人 + 覆盖订阅的读取口径。
//
// LEFT JOIN 让「无覆盖行」也出现在结果里（覆盖缺行由用例按默认表解释）。
// admin 的院校面用 EXISTS 绑定表判定：空绑定即空集合，不会退化成全量。
// 谓词形状与 scopePredicate 同源（master 全量 / admin 交集）。
const adminSubscribersSQL = `
	SELECT u.id::text, u.email,
	       coalesce(p.category, ''), coalesce(p.channel, ''), coalesce(p.enabled, false)
	  FROM users u
	  LEFT JOIN notification_prefs p ON p.user_id = u.id
	 WHERE u.role IN ('master', 'admin')
	   AND u.status = 'active'
	   AND u.deleted_at IS NULL
	   AND (u.role = 'master'
	        OR (NULLIF($1, '') IS NOT NULL
	            AND EXISTS (SELECT 1 FROM admin_schools a
	                         WHERE a.user_id = u.id
	                           AND a.school_id = NULLIF($1, '')::uuid)))
	 ORDER BY u.id, p.category, p.channel`

// AdminSubscribers 返回某校事件的管理员收件人。
// schoolID 为空时只回 master：无院校事件没有可匹配的绑定校。
func (r *NotificationAudienceRepo) AdminSubscribers(ctx context.Context, schoolID string) ([]corenotify.Subscriber, error) {
	rows, err := r.db.q(ctx).Query(ctx, adminSubscribersSQL, schoolID)
	if err != nil {
		return nil, mapError(err)
	}
	defer rows.Close()

	var out []corenotify.Subscriber
	for rows.Next() {
		var (
			userID, email, category, channel string
			enabled                          bool
		)
		if err := rows.Scan(&userID, &email, &category, &channel, &enabled); err != nil {
			return nil, mapError(err)
		}
		if len(out) == 0 || out[len(out)-1].UserID != userID {
			out = append(out, corenotify.Subscriber{
				UserID:    userID,
				Email:     email,
				Overrides: map[corenotify.Pair]bool{},
			})
		}
		if category == "" {
			continue
		}
		pair := corenotify.Pair{
			Category: corenotify.Category(category),
			Channel:  corenotify.Channel(channel),
		}
		// 存储里出现词表外的值时报错，**不静默丢弃**：
		// 丢弃会让一条已存订阅被当成默认值解释。
		if !pair.Category.Valid() {
			return nil, fmt.Errorf("pg: unknown notification category %q in storage", category)
		}
		if !pair.Channel.Valid() {
			return nil, fmt.Errorf("pg: unknown notification channel %q in storage", channel)
		}
		out[len(out)-1].Overrides[pair] = enabled
	}
	if err := rows.Err(); err != nil {
		return nil, mapError(err)
	}
	return out, nil
}

// AccountPhone 返回账号手机号，供通知文案掩码。
// 账号不存在或已软删返回 errs.ErrNotFound；调用方据此放弃该事件。
func (r *NotificationAudienceRepo) AccountPhone(ctx context.Context, accountID string) (string, error) {
	if accountID == "" {
		return "", validationError("account id is required")
	}
	var phone string
	if err := r.db.q(ctx).QueryRow(ctx,
		`SELECT phone FROM accounts WHERE id = $1::uuid AND deleted_at IS NULL`,
		accountID).Scan(&phone); err != nil {
		return "", mapError(err)
	}
	return phone, nil
}

// FailureOwner 返回账号属主的失败邮件开关。
// 只回 active 且未软删的属主；其余一律 not_found，表示没有可投的收件人。
func (r *NotificationAudienceRepo) FailureOwner(ctx context.Context, accountID string) (corenotify.Owner, error) {
	if accountID == "" {
		return corenotify.Owner{}, validationError("account id is required")
	}
	var owner corenotify.Owner
	if err := r.db.q(ctx).QueryRow(ctx, `
		SELECT u.id::text, u.mail_notify
		  FROM accounts a
		  JOIN users u ON u.id = a.owner_user_id
		 WHERE a.id = $1::uuid
		   AND a.deleted_at IS NULL
		   AND u.status = 'active'
		   AND u.deleted_at IS NULL`, accountID).Scan(&owner.UserID, &owner.MailNotify); err != nil {
		return corenotify.Owner{}, mapError(err)
	}
	return owner, nil
}

// RecipientActive 报告用户当前仍是可投收件人：status='active' 且未软删。
// 投递前复核用：入队与发送之间用户可能被停用或软删。
func (r *NotificationAudienceRepo) RecipientActive(ctx context.Context, userID string) (bool, error) {
	if userID == "" {
		return false, validationError("user id is required")
	}
	var active bool
	if err := r.db.q(ctx).QueryRow(ctx, `
		SELECT EXISTS (
		  SELECT 1 FROM users
		   WHERE id = $1::uuid AND status = 'active' AND deleted_at IS NULL)`,
		userID).Scan(&active); err != nil {
		return false, mapError(err)
	}
	return active, nil
}

// ListDueForSend 返回 next_retry_at <= now 的 pending 行，连同通知正文与收件地址。
//
// 只读诊断/入队检查面：本方法不改 claimed_until，**不得用于调度**。
// 调度必须走 ClaimDueForSend，否则多实例会重复投递同一行。
// 读面口径不变：与 ListDue 同一条扫描，多两列读模型（落库文本与收件地址）；
// 收件人集合在 Dispatch 时定型，投递时经 RecipientActive 复核收件人状态。
func (r *NotificationDeliveryRepo) ListDueForSend(ctx context.Context, now time.Time, limit int) ([]corenotify.DueDelivery, error) {
	if now.IsZero() {
		return nil, validationError("due timestamp is required")
	}
	if limit < 1 || limit > 200 {
		return nil, validationError("limit must be between 1 and 200")
	}
	rows, err := r.db.q(ctx).Query(ctx, `
		SELECT d.id::text, d.notification_id::text, d.user_id::text, d.channel,
		       d.status, d.attempts, d.last_error_kind, d.skip_reason, d.next_retry_at,
		       d.created_at, d.updated_at, n.title, n.body, u.email
		  FROM notification_deliveries d
		  JOIN notifications n ON n.id = d.notification_id
		  JOIN users u ON u.id = d.user_id
		 WHERE d.status = 'pending' AND d.next_retry_at IS NOT NULL AND d.next_retry_at <= $1
		 ORDER BY d.next_retry_at, d.id
		 LIMIT $2`, now, limit)
	if err != nil {
		return nil, mapError(err)
	}
	defer rows.Close()

	items := make([]corenotify.DueDelivery, 0, limit)
	for rows.Next() {
		item, err := scanDueDelivery(rows)
		if err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		return nil, mapError(err)
	}
	return items, nil
}

// ClaimDueForSend 认领一批到期投递行，并在同一语句里写入 claimed_until 租约。
//
// 认领是跨实例的调度互斥点：due 子查询用 FOR UPDATE SKIP LOCKED 只锁
// 「pending 且到期且不在租约内」的行，被别的实例锁住的行直接跳过而不是
// 排队等待；claimed 把租约写成 now+lease 后返回认领成功的行。
// 多实例并发时各自拿到不相交的行；崩溃没记终态的行最迟一个租约后重新
// 可见，故投递是至少一次——发送成功后、MarkSent 前崩溃造成的重复外发
// 由该语义接受，账本唯一键不覆盖它。
//
// claimed_until <= now 即视为租约过期（与写入侧 now+lease 同一时钟口径，
// 时间来自调用方注入的 Clock，库内不再取 now() 算租约）。
func (r *NotificationDeliveryRepo) ClaimDueForSend(ctx context.Context,
	now time.Time, limit int, lease time.Duration) ([]corenotify.DueDelivery, error) {
	if now.IsZero() {
		return nil, validationError("due timestamp is required")
	}
	if limit < 1 || limit > 200 {
		return nil, validationError("limit must be between 1 and 200")
	}
	if lease <= 0 || lease > 24*time.Hour {
		return nil, validationError("lease must be within (0, 24h]")
	}
	rows, err := r.db.q(ctx).Query(ctx, `
		WITH due AS (
			SELECT id FROM notification_deliveries
			 WHERE status = 'pending' AND next_retry_at IS NOT NULL AND next_retry_at <= $1
			   AND (claimed_until IS NULL OR claimed_until <= $1)
			 ORDER BY next_retry_at, id
			 LIMIT $2
			 FOR UPDATE SKIP LOCKED
		), claimed AS (
			UPDATE notification_deliveries d
			   SET claimed_until = $1::timestamptz + $3::interval,
			       updated_at = now()
			  FROM due
			 WHERE d.id = due.id
			 RETURNING d.id, d.notification_id, d.user_id, d.channel,
			           d.status, d.attempts, d.last_error_kind, d.skip_reason, d.next_retry_at,
			           d.created_at, d.updated_at
		)
		SELECT c.id::text, c.notification_id::text, c.user_id::text, c.channel, c.status, c.attempts,
		       c.last_error_kind, c.skip_reason, c.next_retry_at, c.created_at, c.updated_at,
		       n.title, n.body, u.email
		  FROM claimed c
		  JOIN notifications n ON n.id = c.notification_id
		  JOIN users u ON u.id = c.user_id
		 ORDER BY c.next_retry_at, c.id`, now, limit, lease.String())
	if err != nil {
		return nil, mapError(err)
	}
	defer rows.Close()

	items := make([]corenotify.DueDelivery, 0, limit)
	for rows.Next() {
		item, err := scanDueDelivery(rows)
		if err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		return nil, mapError(err)
	}
	return items, nil
}

// ReleaseClaim 归还一条行的认领：只清 claimed_until，不改 status、
// attempts 与 next_retry_at。
//
// 发送前的瞬时读错误（RecipientActive、CountSentOnDay）走这里，让行在
// 下一轮 30 秒内即可重新认领，而不是等满租约。释放幂等：对未认领行或
// 终态行是空操作（WHERE status = 'pending' 兜住并发终态记账）。
func (r *NotificationDeliveryRepo) ReleaseClaim(ctx context.Context, id string) error {
	if id == "" {
		return validationError("delivery id is required")
	}
	if _, err := r.db.Exec(ctx, `
		UPDATE notification_deliveries
		   SET claimed_until = NULL, updated_at = now()
		 WHERE id = $1::uuid AND status = 'pending'`, id); err != nil {
		return err
	}
	return nil
}

// scanDueDelivery 读一行到期投递（账本列 + 通知正文 + 收件地址）。
// 列序是 ListDueForSend 与 ClaimDueForSend 的公共读模型。
func scanDueDelivery(row pgx.Row) (corenotify.DueDelivery, error) {
	var (
		item corenotify.DueDelivery
		next pgtype.Timestamptz
	)
	if err := row.Scan(&item.ID, &item.NotificationID, &item.UserID, &item.Channel,
		&item.Status, &item.Attempts, &item.LastErrorKind, &item.SkipReason, &next,
		&item.CreatedAt, &item.UpdatedAt, &item.Title, &item.Body, &item.Email); err != nil {
		return corenotify.DueDelivery{}, mapError(err)
	}
	item.NextRetryAt = timePtr(next)
	return item, nil
}
