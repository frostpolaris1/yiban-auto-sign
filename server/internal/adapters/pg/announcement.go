// 全站公告的 PostgreSQL 适配器。
//
// 功能：读、写、清空 settings 表里的公告行。
// 本文件**不**判角色、**不**写审计；审计由 app/settings 在同一事务追加。
//
// 归属：adapters 层。app/settings 只依赖 Store 端口，不见本类型。
//
// 口径：公告存现有 settings 表的 announcement.text 键。
// 清空等价于删除行，故「从未设置」与「已清空」读到同一状态：
// 空正文与 nil 时间。写入走单语句 upsert。

package pg

import (
	"context"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"
)

// settingsAnnouncementKey 是公告正文的键。
const settingsAnnouncementKey = "announcement.text"

// AnnouncementRepo 读写全站公告。
type AnnouncementRepo struct {
	db *DB
}

// NewAnnouncementRepo 构造公告仓储。
func NewAnnouncementRepo(db *DB) *AnnouncementRepo {
	return &AnnouncementRepo{db: db}
}

// Announcement 返回当前公告。行缺失时返回空正文与 nil 时间，不报 not_found。
func (r *AnnouncementRepo) Announcement(ctx context.Context) (string, *time.Time, error) {
	var (
		text      string
		updatedAt time.Time
	)
	err := r.db.q(ctx).QueryRow(ctx,
		`SELECT value, updated_at FROM settings WHERE key = $1`, settingsAnnouncementKey).
		Scan(&text, &updatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", nil, nil
	}
	if err != nil {
		return "", nil, mapError(err)
	}
	return text, &updatedAt, nil
}

// SetAnnouncement 写入公告并返回更新时间；空正文删除该行并返回 nil 时间。
// 调用方必须在事务内调用，使本次写入与审计同存亡。
func (r *AnnouncementRepo) SetAnnouncement(ctx context.Context, text, updatedBy string) (*time.Time, error) {
	if updatedBy == "" {
		return nil, validationError("actor user id is required")
	}
	if text == "" {
		if _, err := r.db.q(ctx).Exec(ctx,
			`DELETE FROM settings WHERE key = $1`, settingsAnnouncementKey); err != nil {
			return nil, mapError(err)
		}
		return nil, nil
	}
	var updatedAt time.Time
	err := r.db.q(ctx).QueryRow(ctx, `
		INSERT INTO settings (key, value, updated_by)
		VALUES ($1, $2, $3::uuid)
		ON CONFLICT (key) DO UPDATE
		   SET value = EXCLUDED.value,
		       updated_by = EXCLUDED.updated_by
		RETURNING updated_at`,
		settingsAnnouncementKey, text, updatedBy).Scan(&updatedAt)
	if err != nil {
		return nil, mapError(err)
	}
	return &updatedAt, nil
}
