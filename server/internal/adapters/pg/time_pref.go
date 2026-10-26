package pg

import (
	"context"
	"time"

	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
)

// TimePrefRepo 读写用户自选时间片（time_prefs）。
//
// 表以 user_id 为主键：一个用户一条偏好，作用于其名下账号。
// offset_min 为 NULL 表示"无偏好"；该行保留只为记住最近更新时间，
// 冷却判定据此计算。
type TimePrefRepo struct {
	db *DB
}

// NewTimePrefRepo 构造自选仓储。
func NewTimePrefRepo(db *DB) *TimePrefRepo { return &TimePrefRepo{db: db} }

// Get 读一个用户的自选；不存在返回 errs.ErrNotFound。
func (r *TimePrefRepo) Get(ctx context.Context, userID string) (coreschedule.TimePref, error) {
	var p coreschedule.TimePref
	err := r.db.q(ctx).QueryRow(ctx,
		`SELECT user_id::text, offset_min, updated_at FROM time_prefs WHERE user_id = $1::uuid`,
		userID).Scan(&p.UserID, &p.OffsetMin, &p.UpdatedAt)
	if err != nil {
		return coreschedule.TimePref{}, mapError(err)
	}
	return p, nil
}

// Put 覆盖写一个用户的自选。offset 为 nil 时写 NULL（清除偏好）。
func (r *TimePrefRepo) Put(ctx context.Context, userID string, offset *int, now time.Time) error {
	_, err := r.db.q(ctx).Exec(ctx,
		`INSERT INTO time_prefs (user_id, offset_min, updated_at) VALUES ($1::uuid, $2, $3)
		 ON CONFLICT (user_id) DO UPDATE SET offset_min = EXCLUDED.offset_min, updated_at = EXCLUDED.updated_at`,
		userID, offset, now)
	if err != nil {
		return mapInsertForeignKey(err)
	}
	return nil
}

// Offsets 批量读一组用户的有效偏好，返回 userID → offset_min。
// 无行或 offset 为 NULL 的用户不出现在结果里。
func (r *TimePrefRepo) Offsets(ctx context.Context, userIDs []string) (map[string]int, error) {
	out := make(map[string]int, len(userIDs))
	if len(userIDs) == 0 {
		return out, nil
	}
	rows, err := r.db.q(ctx).Query(ctx,
		`SELECT user_id::text, offset_min FROM time_prefs
		 WHERE user_id = ANY($1::text[]::uuid[]) AND offset_min IS NOT NULL`,
		userIDs)
	if err != nil {
		return nil, mapError(err)
	}
	defer rows.Close()
	for rows.Next() {
		var id string
		var off int
		if err := rows.Scan(&id, &off); err != nil {
			return nil, mapError(err)
		}
		out[id] = off
	}
	return out, mapError(rows.Err())
}
