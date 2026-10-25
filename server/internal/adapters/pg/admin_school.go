package pg

import (
	"context"
	"strings"

	coreschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/school"
)

// AdminSchoolRepo 是 PostgreSQL 管理员绑定仓储。
type AdminSchoolRepo struct {
	db *DB
}

// NewAdminSchoolRepo 构造管理员绑定仓储。
func NewAdminSchoolRepo(db *DB) *AdminSchoolRepo { return &AdminSchoolRepo{db: db} }

// Grant 绑定用户与院校。重复授予同一条绑定是空操作。
func (r *AdminSchoolRepo) Grant(ctx context.Context, binding coreschool.AdminBinding) error {
	if strings.TrimSpace(binding.UserID) == "" || strings.TrimSpace(binding.SchoolID) == "" {
		return validationError("user and school are required")
	}
	_, err := r.db.q(ctx).Exec(ctx, `
		INSERT INTO admin_schools (user_id, school_id, granted_by)
		VALUES ($1::uuid, $2::uuid, NULLIF($3, '')::uuid)
		ON CONFLICT (user_id, school_id) DO NOTHING`,
		binding.UserID, binding.SchoolID, binding.GrantedBy)
	return mapError(err)
}

// Revoke 删除一条绑定。无行可删时报 not_found。
func (r *AdminSchoolRepo) Revoke(ctx context.Context, userID, schoolID string) error {
	tag, err := r.db.q(ctx).Exec(ctx, `
		DELETE FROM admin_schools WHERE user_id = $1::uuid AND school_id = $2::uuid`,
		userID, schoolID)
	if err != nil {
		return mapError(err)
	}
	if tag.RowsAffected() == 0 {
		return coreerrNotFound()
	}
	return nil
}

// SchoolsForUser 列出用户绑定的院校。
// 空结果表示该用户不管理任何院校，**绝不解释为「全部院校」**。
func (r *AdminSchoolRepo) SchoolsForUser(ctx context.Context, userID string) ([]string, error) {
	rows, err := r.db.q(ctx).Query(ctx, `
		SELECT school_id::text FROM admin_schools
		 WHERE user_id = $1::uuid ORDER BY school_id`, userID)
	if err != nil {
		return nil, mapError(err)
	}
	defer rows.Close()

	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, mapError(err)
		}
		ids = append(ids, id)
	}
	if err := rows.Err(); err != nil {
		return nil, mapError(err)
	}
	return ids, nil
}
