package pg

import (
	"context"

	corelocation "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/location"
)

// LocationRepo 是账号定位策略的仓储（P5-C）。
// 一行一账号；auto 行的 lng/lat 为 NULL。
type LocationRepo struct {
	db *DB
}

// NewLocationRepo 构造定位仓储。
func NewLocationRepo(db *DB) *LocationRepo { return &LocationRepo{db: db} }

// Get 读一行定位偏好；无行返回 errs.ErrNotFound。
func (r *LocationRepo) Get(ctx context.Context, accountID string) (corelocation.Row, error) {
	var row corelocation.Row
	var lng, lat *float64
	var updatedBy *string
	err := r.db.q(ctx).QueryRow(ctx,
		`SELECT account_id::text, mode, lng, lat, reason, updated_by::text, updated_at
		 FROM account_location WHERE account_id = $1::uuid`, accountID).
		Scan(&row.AccountID, &row.Mode, &lng, &lat, &row.Reason, &updatedBy, &row.UpdatedAt)
	if err != nil {
		return corelocation.Row{}, mapError(err)
	}
	// auto 行坐标为 NULL：归一为零值，读方只信 mode。
	if lng != nil {
		row.Lng = *lng
	}
	if lat != nil {
		row.Lat = *lat
	}
	if updatedBy != nil {
		row.UpdatedBy = *updatedBy
	}
	return row, nil
}

// Set 覆盖写一行定位偏好。Point 为 nil 表示 auto（坐标清空）。
// updated_at 由本语句显式写：测试用假时钟时不能依赖 now() 缺省。
func (r *LocationRepo) Set(ctx context.Context, in corelocation.Pref) error {
	var lng, lat any
	if in.Point != nil {
		lng, lat = in.Point.Lng, in.Point.Lat
	}
	var updatedBy any
	if in.UpdatedBy != "" {
		updatedBy = in.UpdatedBy
	}
	_, err := r.db.q(ctx).Exec(ctx,
		`INSERT INTO account_location (account_id, mode, lng, lat, reason, updated_by, updated_at)
		 VALUES ($1::uuid, $2, $3, $4, $5, $6::uuid, $7)
		 ON CONFLICT (account_id) DO UPDATE SET
		   mode = EXCLUDED.mode, lng = EXCLUDED.lng, lat = EXCLUDED.lat,
		   reason = EXCLUDED.reason, updated_by = EXCLUDED.updated_by,
		   updated_at = EXCLUDED.updated_at`,
		in.AccountID, in.Mode, lng, lat, in.Reason, updatedBy, in.Now.UTC())
	return mapInsertForeignKey(err)
}
