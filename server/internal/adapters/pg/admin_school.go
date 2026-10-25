package pg

import (
	"context"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
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

// bindingColumns 是绑定行与用户信息的投影。
// granted_by 可能为空（授予者已删除），故取空串。
const bindingColumns = `a.user_id::text, a.school_id::text,
	coalesce(a.granted_by::text, ''), a.created_at, u.email, u.role::text`

// ListBySchool 列某院校的管理员，游标分页。
// 已软删用户不出现在列表里；他的绑定行仍在，解绑仍然可用。
func (r *AdminSchoolRepo) ListBySchool(ctx context.Context, schoolID, cursor string, limit int) ([]coreschool.AdminBinding, string, error) {
	if limit < 1 || limit > 200 {
		return nil, "", validationError("limit must be between 1 and 200")
	}
	query := `SELECT ` + bindingColumns + `
		  FROM admin_schools a
		  JOIN users u ON u.id = a.user_id
		 WHERE a.school_id = $1::uuid AND u.deleted_at IS NULL`
	args := []any{schoolID}
	var err error
	query, args, err = appendKeyset(query, args, cursor, "a.created_at", "a.user_id")
	if err != nil {
		return nil, "", err
	}
	query += ` ORDER BY a.created_at DESC, a.user_id DESC`
	query, args = appendLimit(query, args, limit)

	rows, err := r.db.q(ctx).Query(ctx, query, args...)
	if err != nil {
		return nil, "", mapError(err)
	}
	defer rows.Close()

	items := make([]coreschool.AdminBinding, 0, limit)
	for rows.Next() {
		binding, err := scanBinding(rows)
		if err != nil {
			return nil, "", mapError(err)
		}
		items = append(items, binding)
	}
	if err := rows.Err(); err != nil {
		return nil, "", mapError(err)
	}
	more := len(items) > limit
	if more {
		items = items[:limit]
	}
	next := nextCursor(items, more, func(binding coreschool.AdminBinding) (time.Time, string) {
		return binding.CreatedAt, binding.UserID
	})
	return items, next, nil
}

// GetBinding 返回一条绑定，附带用户邮箱与角色。
// 用户已软删时按 not_found 处理。
func (r *AdminSchoolRepo) GetBinding(ctx context.Context, userID, schoolID string) (coreschool.AdminBinding, error) {
	row := r.db.q(ctx).QueryRow(ctx, `SELECT `+bindingColumns+`
		  FROM admin_schools a
		  JOIN users u ON u.id = a.user_id
		 WHERE a.user_id = $1::uuid AND a.school_id = $2::uuid AND u.deleted_at IS NULL`,
		userID, schoolID)
	return recordOrNotFound(scanBinding(row))
}

func scanBinding(row pgx.Row) (coreschool.AdminBinding, error) {
	var (
		binding coreschool.AdminBinding
		role    string
	)
	if err := row.Scan(&binding.UserID, &binding.SchoolID, &binding.GrantedBy,
		&binding.CreatedAt, &binding.Email, &role); err != nil {
		return coreschool.AdminBinding{}, err
	}
	binding.Role = coreauth.Role(role)
	return binding, nil
}
