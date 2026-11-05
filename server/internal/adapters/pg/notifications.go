package pg

// 站内信与投递账本的 PostgreSQL 适配器。
//
// 功能：站内信列表（键集分页）、未读数、标记已读、全部已读、
// 30 天清理、通知与账本的同事务写入（InsertWithPending），
// 以及投递账本的入队、到期扫描与终态记账。
// 本文件不判角色、不拼通知文案、不投递邮件、不写审计。
//
// 归属：adapters 层。app/notify 只依赖端口，不见本类型。
//
// 口径：所有用户面查询都以 user_id 为第一谓词；越权 id 命中 0 行，
// 不报错（幂等）。列表游标复用 cursor.go 的 (时刻, uuid) 形状；
// 排序键固定 (created_at DESC, id DESC)。
// 通知的类别与账本的渠道在本层只做取值校验，文本掩码由用例负责。

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
)

// NotificationRepo 读写 notifications 表。
type NotificationRepo struct{ db *DB }

// NewNotificationRepo 构造站内信仓储。
func NewNotificationRepo(db *DB) *NotificationRepo { return &NotificationRepo{db: db} }

// notificationColumns 是读模型列，顺序与 scanNotification 一致。
const notificationColumns = `id::text, category, title, body,
	coalesce(school_id::text, ''), created_at, read_at`

// List 读取一个用户的一页站内信，按 (created_at DESC, id DESC) 键集分页。
// limit 必须是 1..100；游标非法即校验失败。
func (r *NotificationRepo) List(ctx context.Context, query corenotify.NotificationQuery) ([]corenotify.Notification, string, error) {
	if query.UserID == "" {
		return nil, "", validationError("user id is required")
	}
	if query.Limit < 1 || query.Limit > 100 {
		return nil, "", validationError("limit must be between 1 and 100")
	}
	where := ` WHERE user_id = $1::uuid`
	args := []any{query.UserID}
	var err error
	where, args, err = appendKeyset(where, args, query.Cursor, "created_at", "id")
	if err != nil {
		return nil, "", err
	}
	sqlText := `SELECT ` + notificationColumns + ` FROM notifications` + where +
		` ORDER BY created_at DESC, id DESC`
	sqlText, args = appendLimit(sqlText, args, query.Limit)

	rows, err := r.db.q(ctx).Query(ctx, sqlText, args...)
	if err != nil {
		return nil, "", mapError(err)
	}
	defer rows.Close()

	items := make([]corenotify.Notification, 0, query.Limit)
	for rows.Next() {
		item, err := scanNotification(rows)
		if err != nil {
			return nil, "", err
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		return nil, "", mapError(err)
	}
	more := len(items) > query.Limit
	if more {
		items = items[:query.Limit]
	}
	next := nextCursor(items, more, func(item corenotify.Notification) (time.Time, string) {
		return item.CreatedAt, item.ID
	})
	return items, next, nil
}

// UnreadCount 返回一个用户的未读数。本人作用域是查询的一部分。
func (r *NotificationRepo) UnreadCount(ctx context.Context, userID string) (int64, error) {
	if userID == "" {
		return 0, validationError("user id is required")
	}
	var count int64
	if err := r.db.q(ctx).QueryRow(ctx,
		`SELECT count(*) FROM notifications
		  WHERE user_id = $1::uuid AND read_at IS NULL`, userID).Scan(&count); err != nil {
		return 0, mapError(err)
	}
	return count, nil
}

// MarkRead 把属于 userID 的指定行标为已读，返回实际更新行数。
// 越权或不存在 id 命中 0 行，不报错：调用方按幂等处理。
// 已读行不重写 read_at：首次已读时刻是语义值。
func (r *NotificationRepo) MarkRead(ctx context.Context, userID string, ids []string, now time.Time) (int64, error) {
	if userID == "" {
		return 0, validationError("user id is required")
	}
	if len(ids) == 0 {
		return 0, validationError("ids is required")
	}
	if now.IsZero() {
		return 0, validationError("read timestamp is required")
	}
	ct, err := r.db.q(ctx).Exec(ctx, `
		UPDATE notifications
		   SET read_at = $3
		 WHERE user_id = $1::uuid
		   AND id = ANY($2::uuid[])
		   AND read_at IS NULL`, userID, ids, now)
	if err != nil {
		return 0, mapError(err)
	}
	return ct.RowsAffected(), nil
}

// MarkAllRead 把一个用户的全部未读行标为已读，返回实际更新行数。
func (r *NotificationRepo) MarkAllRead(ctx context.Context, userID string, now time.Time) (int64, error) {
	if userID == "" {
		return 0, validationError("user id is required")
	}
	if now.IsZero() {
		return 0, validationError("read timestamp is required")
	}
	ct, err := r.db.q(ctx).Exec(ctx, `
		UPDATE notifications
		   SET read_at = $2
		 WHERE user_id = $1::uuid AND read_at IS NULL`, userID, now)
	if err != nil {
		return 0, mapError(err)
	}
	return ct.RowsAffected(), nil
}

// deleteNotificationsBefore 是站内信保留期删除的唯一 SQL 形状。
// 维护仓储是唯一调用方；通知仓储不再重复暴露清理方法。
func deleteNotificationsBefore(ctx context.Context, db *DB, before time.Time, limit int) (int64, error) {
	ct, err := db.q(ctx).Exec(ctx, `
		DELETE FROM notifications WHERE id IN (
		  SELECT id FROM notifications WHERE created_at < $1
		   ORDER BY created_at, id LIMIT $2)`, before, limit)
	if err != nil {
		return 0, mapError(err)
	}
	return ct.RowsAffected(), nil
}

// Insert 写入一条站内信并返回落库行。
// (user_id, dedupe_key) 冲突时返回已存在的行：同一业务事件只落一行，
// 调用方不需要区分「本次创建」与「重放」。
func (r *NotificationRepo) Insert(ctx context.Context, in corenotify.NotificationInsert) (corenotify.Notification, error) {
	if in.UserID == "" {
		return corenotify.Notification{}, validationError("user id is required")
	}
	if !in.Category.Valid() {
		return corenotify.Notification{}, validationError("unknown notification category")
	}
	if in.DedupeKey == "" {
		return corenotify.Notification{}, validationError("dedupe key is required")
	}
	if in.CreatedAt.IsZero() {
		return corenotify.Notification{}, validationError("created timestamp is required")
	}
	var (
		item   corenotify.Notification
		readAt pgtype.Timestamptz
	)
	err := r.db.q(ctx).QueryRow(ctx, `
		INSERT INTO notifications
		       (user_id, category, dedupe_key, title, body, school_id, created_at)
		VALUES ($1::uuid, $2, $3, $4, $5, NULLIF($6, '')::uuid, $7)
		ON CONFLICT (user_id, dedupe_key) DO NOTHING
		RETURNING `+notificationColumns,
		in.UserID, string(in.Category), in.DedupeKey, in.Title, in.Body, in.SchoolID, in.CreatedAt).
		Scan(&item.ID, &item.Category, &item.Title, &item.Body, &item.SchoolID,
			&item.CreatedAt, &readAt)
	if err == nil {
		item.ReadAt = timePtr(readAt)
		return item, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return corenotify.Notification{}, mapInsertForeignKey(err)
	}
	// 唯一键冲突走独立语句：新语句取新快照，能看到并发事务已提交的行。
	err = r.db.q(ctx).QueryRow(ctx, `
		SELECT `+notificationColumns+` FROM notifications
		 WHERE user_id = $1::uuid AND dedupe_key = $2`,
		in.UserID, in.DedupeKey).
		Scan(&item.ID, &item.Category, &item.Title, &item.Body, &item.SchoolID,
			&item.CreatedAt, &readAt)
	if err != nil {
		return corenotify.Notification{}, mapError(err)
	}
	item.ReadAt = timePtr(readAt)
	return item, nil
}

// InsertWithPending 在同一事务里写站内信与待投递账本行。
//
// 通知的 (user_id, dedupe_key) 与账本的 (notification_id, channel, user_id)
// 各有一道唯一键：重复事件返回已存在的两行，不产生第二份。
// pending 的 NotificationID/UserID 由本方法按通知行填入，调用方留空。
// 原子性保证「有通知必有账本」：中途崩溃不会留下没有账本行的邮件事件。
func (r *NotificationRepo) InsertWithPending(ctx context.Context, note corenotify.NotificationInsert,
	pending corenotify.DeliveryInsert) (corenotify.NotificationWithDelivery, error) {
	var out corenotify.NotificationWithDelivery
	err := r.db.InTx(ctx, func(ctx context.Context) error {
		notification, err := r.Insert(ctx, note)
		if err != nil {
			return err
		}
		pending.NotificationID = notification.ID
		pending.UserID = note.UserID
		delivery, err := insertPending(ctx, r.db.q(ctx), pending)
		if err != nil {
			return err
		}
		out = corenotify.NotificationWithDelivery{Notification: notification, Delivery: delivery}
		return nil
	})
	if err != nil {
		return corenotify.NotificationWithDelivery{}, err
	}
	return out, nil
}

// scanNotification 读一行通知。类别不在词表内即报错，不静默降级：
// 界面把未知类别显示成任意一类都是错误事实。
func scanNotification(row pgx.Row) (corenotify.Notification, error) {
	var (
		item   corenotify.Notification
		readAt pgtype.Timestamptz
	)
	if err := row.Scan(&item.ID, &item.Category, &item.Title, &item.Body,
		&item.SchoolID, &item.CreatedAt, &readAt); err != nil {
		return corenotify.Notification{}, mapError(err)
	}
	if !item.Category.Valid() {
		return corenotify.Notification{}, fmt.Errorf(
			"pg: unknown notification category %q in storage", item.Category)
	}
	item.ReadAt = timePtr(readAt)
	return item, nil
}

// NotificationDeliveryRepo 读写 notification_deliveries 表。
type NotificationDeliveryRepo struct{ db *DB }

// NewNotificationDeliveryRepo 构造投递账本仓储。
func NewNotificationDeliveryRepo(db *DB) *NotificationDeliveryRepo {
	return &NotificationDeliveryRepo{db: db}
}

// deliveryColumns 是账本读模型列，顺序与 scanDelivery 一致。
const deliveryColumns = `id::text, notification_id::text, user_id::text, channel,
	status, attempts, last_error_kind, skip_reason, next_retry_at, created_at, updated_at`

// InsertPending 插入一条待投递行并返回落库行。
// (notification_id, channel, user_id) 冲突时返回已存在的行：
// 重复事件与并发双插都只留一行。
func (r *NotificationDeliveryRepo) InsertPending(ctx context.Context, in corenotify.DeliveryInsert) (corenotify.Delivery, error) {
	return insertPending(ctx, r.db.q(ctx), in)
}

// insertPending 是插入账本行的唯一 SQL 形状。
// q 可以是连接池，也可以是事务：通知仓储的 InsertWithPending 复用同一实现，
// 保证两条写入路径的列集与冲突口径只有一份。
func insertPending(ctx context.Context, q queryer, in corenotify.DeliveryInsert) (corenotify.Delivery, error) {
	if in.NotificationID == "" {
		return corenotify.Delivery{}, validationError("notification id is required")
	}
	if in.UserID == "" {
		return corenotify.Delivery{}, validationError("user id is required")
	}
	if in.Channel != corenotify.ChannelEmail {
		return corenotify.Delivery{}, validationError("delivery channel must be email")
	}
	var (
		item corenotify.Delivery
		next pgtype.Timestamptz
	)
	err := q.QueryRow(ctx, `
		INSERT INTO notification_deliveries
		       (notification_id, user_id, channel, next_retry_at)
		VALUES ($1::uuid, $2::uuid, $3, coalesce($4, now()))
		ON CONFLICT (notification_id, channel, user_id) DO NOTHING
		RETURNING `+deliveryColumns,
		in.NotificationID, in.UserID, string(in.Channel), in.NextRetryAt).
		Scan(&item.ID, &item.NotificationID, &item.UserID, &item.Channel, &item.Status,
			&item.Attempts, &item.LastErrorKind, &item.SkipReason, &next,
			&item.CreatedAt, &item.UpdatedAt)
	if err == nil {
		item.NextRetryAt = timePtr(next)
		return item, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return corenotify.Delivery{}, mapInsertForeignKey(err)
	}
	// 唯一键冲突走独立语句：新语句取新快照，能看到并发事务已提交的行。
	err = q.QueryRow(ctx, `
		SELECT `+deliveryColumns+` FROM notification_deliveries
		 WHERE notification_id = $1::uuid AND channel = $2 AND user_id = $3::uuid`,
		in.NotificationID, string(in.Channel), in.UserID).
		Scan(&item.ID, &item.NotificationID, &item.UserID, &item.Channel, &item.Status,
			&item.Attempts, &item.LastErrorKind, &item.SkipReason, &next,
			&item.CreatedAt, &item.UpdatedAt)
	if err != nil {
		return corenotify.Delivery{}, mapError(err)
	}
	item.NextRetryAt = timePtr(next)
	return item, nil
}

// ListDue 返回 next_retry_at <= now 的 pending 行，按到期时刻升序。
// 单进程串行投递，故不需要 SKIP LOCKED。
func (r *NotificationDeliveryRepo) ListDue(ctx context.Context, now time.Time, limit int) ([]corenotify.Delivery, error) {
	if now.IsZero() {
		return nil, validationError("due timestamp is required")
	}
	if limit < 1 || limit > 200 {
		return nil, validationError("limit must be between 1 and 200")
	}
	rows, err := r.db.q(ctx).Query(ctx, `
		SELECT `+deliveryColumns+` FROM notification_deliveries
		 WHERE status = 'pending' AND next_retry_at IS NOT NULL AND next_retry_at <= $1
		 ORDER BY next_retry_at, id
		 LIMIT $2`, now, limit)
	if err != nil {
		return nil, mapError(err)
	}
	defer rows.Close()

	items := make([]corenotify.Delivery, 0, limit)
	for rows.Next() {
		item, err := scanDelivery(rows)
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

// MarkSent 把一条待投递行记为已发送。已进入终态的行不再改写。
func (r *NotificationDeliveryRepo) MarkSent(ctx context.Context, id string) error {
	return r.markTerminal(ctx, id, `
		UPDATE notification_deliveries
		   SET status = 'sent', attempts = attempts + 1, last_error_kind = '',
		       skip_reason = '', next_retry_at = NULL, updated_at = now()
		 WHERE id = $1::uuid AND status = 'pending'`)
}

// MarkFailed 记一次失败。
// nextRetryAt 非空时保持 pending 并安排下一次重试；为空时落 failed。
// attempts 每次失败都递增，重试上限由调用方判定。
func (r *NotificationDeliveryRepo) MarkFailed(ctx context.Context, id, errorKind string, nextRetryAt *time.Time) error {
	if id == "" {
		return validationError("delivery id is required")
	}
	_, err := r.db.q(ctx).Exec(ctx, `
		UPDATE notification_deliveries
		   SET status = CASE WHEN $3::timestamptz IS NULL THEN 'failed' ELSE 'pending' END,
		       attempts = attempts + 1,
		       last_error_kind = $2,
		       skip_reason = '',
		       next_retry_at = $3,
		       updated_at = now()
		 WHERE id = $1::uuid AND status = 'pending'`, id, errorKind, nextRetryAt)
	return mapError(err)
}

// MarkSkipped 把一条待投递行记为跳过。
// 跳过不是发送尝试，故 attempts 不递增；原因写 skip_reason，
// last_error_kind 清空——后者只存 core/mail.Kind 词表。
func (r *NotificationDeliveryRepo) MarkSkipped(ctx context.Context, id, reason string) error {
	return r.markTerminal(ctx, id, `
		UPDATE notification_deliveries
		   SET status = 'skipped', skip_reason = $2, last_error_kind = '',
		       next_retry_at = NULL, updated_at = now()
		 WHERE id = $1::uuid AND status = 'pending'`, reason)
}

// markTerminal 执行一条只改 pending 行的终态语句。0 行不是错误：幂等重放。
func (r *NotificationDeliveryRepo) markTerminal(ctx context.Context, id, sqlText string, args ...any) error {
	if id == "" {
		return validationError("delivery id is required")
	}
	all := append([]any{id}, args...)
	if _, err := r.db.q(ctx).Exec(ctx, sqlText, all...); err != nil {
		return mapError(err)
	}
	return nil
}

// scanDelivery 读一行账本。
func scanDelivery(row pgx.Row) (corenotify.Delivery, error) {
	var (
		item corenotify.Delivery
		next pgtype.Timestamptz
	)
	if err := row.Scan(&item.ID, &item.NotificationID, &item.UserID, &item.Channel,
		&item.Status, &item.Attempts, &item.LastErrorKind, &item.SkipReason, &next,
		&item.CreatedAt, &item.UpdatedAt); err != nil {
		return corenotify.Delivery{}, mapError(err)
	}
	item.NextRetryAt = timePtr(next)
	return item, nil
}
