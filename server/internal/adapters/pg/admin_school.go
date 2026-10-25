package pg

import (
	"context"
	"strings"

	coreschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/school"
)

// AdminSchoolRepo is the PostgreSQL administrator-binding repository.
type AdminSchoolRepo struct {
	db *DB
}

// NewAdminSchoolRepo builds an administrator-binding repository.
func NewAdminSchoolRepo(db *DB) *AdminSchoolRepo { return &AdminSchoolRepo{db: db} }

// Grant binds a user to a school. Granting an existing binding is a no-op.
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

// Revoke deletes one binding.
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

// SchoolsForUser lists the schools bound to a user. An empty result means the
// user administers no school; it is never treated as "all schools".
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
