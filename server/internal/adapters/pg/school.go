package pg

import (
	"context"

	"github.com/jackc/pgx/v5"

	coreschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/school"
)

// SchoolRepo 是 PostgreSQL 院校仓储。
type SchoolRepo struct {
	db *DB
}

// NewSchoolRepo 构造院校仓储。
func NewSchoolRepo(db *DB) *SchoolRepo { return &SchoolRepo{db: db} }

const schoolColumns = `id::text, code, name, status::text, time_zone, created_at, updated_at`

// Create 插入院校。空时区套用列默认值。
func (r *SchoolRepo) Create(ctx context.Context, in coreschool.New) (coreschool.Record, error) {
	if in.Code == "" || in.Name == "" {
		return coreschool.Record{}, validationError("code and name are required")
	}
	id := in.ID
	if id == "" {
		var err error
		id, err = newID()
		if err != nil {
			return coreschool.Record{}, err
		}
	}
	var (
		row pgx.Row
		q   = r.db.q(ctx)
	)
	if in.TimeZone == "" {
		row = q.QueryRow(ctx, `
			INSERT INTO schools (id, code, name) VALUES ($1::uuid, $2, $3)
			RETURNING `+schoolColumns, id, in.Code, in.Name)
	} else {
		row = q.QueryRow(ctx, `
			INSERT INTO schools (id, code, name, time_zone) VALUES ($1::uuid, $2, $3, $4)
			RETURNING `+schoolColumns, id, in.Code, in.Name, in.TimeZone)
	}
	return recordOrNotFound(scanSchool(row))
}

// Get 返回一个院校。
func (r *SchoolRepo) Get(ctx context.Context, id string) (coreschool.Record, error) {
	row := r.db.q(ctx).QueryRow(ctx,
		`SELECT `+schoolColumns+` FROM schools WHERE id = $1::uuid`, id)
	return recordOrNotFound(scanSchool(row))
}

// GetByCode 按对外编码返回院校。
func (r *SchoolRepo) GetByCode(ctx context.Context, code string) (coreschool.Record, error) {
	row := r.db.q(ctx).QueryRow(ctx,
		`SELECT `+schoolColumns+` FROM schools WHERE code = $1`, code)
	return recordOrNotFound(scanSchool(row))
}

// Update 修改 in 中出现的字段。nil 字段保持原值。
func (r *SchoolRepo) Update(ctx context.Context, id string, in coreschool.Update) (coreschool.Record, error) {
	var status *string
	if in.Status != nil {
		if !in.Status.Valid() {
			return coreschool.Record{}, validationError("unknown school status")
		}
		value := string(*in.Status)
		status = &value
	}
	row := r.db.q(ctx).QueryRow(ctx, `
		UPDATE schools
		   SET name = COALESCE($2, name),
		       status = COALESCE($3::school_status, status),
		       time_zone = COALESCE($4, time_zone),
		       updated_at = now()
		 WHERE id = $1::uuid
		RETURNING `+schoolColumns, id, in.Name, status, in.TimeZone)
	return recordOrNotFound(scanSchool(row))
}

func scanSchool(row pgx.Row) (coreschool.Record, error) {
	var (
		record coreschool.Record
		status string
	)
	if err := row.Scan(&record.ID, &record.Code, &record.Name, &status,
		&record.TimeZone, &record.CreatedAt, &record.UpdatedAt); err != nil {
		return coreschool.Record{}, err
	}
	record.Status = coreschool.Status(status)
	return record, nil
}
