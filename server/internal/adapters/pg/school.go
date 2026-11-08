package pg

import (
	"context"
	"time"

	"github.com/jackc/pgx/v5"

	coreschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/school"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// SchoolRepo 是 PostgreSQL 院校仓储。
type SchoolRepo struct {
	db *DB
}

// NewSchoolRepo 构造院校仓储。
func NewSchoolRepo(db *DB) *SchoolRepo { return &SchoolRepo{db: db} }

const schoolColumns = `id::text, code, name, status::text, time_zone,
	coalesce(platform_university_id, ''), created_at, updated_at`

// Create 插入院校。空时区套用列默认值；空平台绑定落 NULL。
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
			INSERT INTO schools (id, code, name, platform_university_id)
			VALUES ($1::uuid, $2, $3, NULLIF($4, ''))
			RETURNING `+schoolColumns, id, in.Code, in.Name, in.PlatformUniversityID)
	} else {
		row = q.QueryRow(ctx, `
			INSERT INTO schools (id, code, name, time_zone, platform_university_id)
			VALUES ($1::uuid, $2, $3, $4, NULLIF($5, ''))
			RETURNING `+schoolColumns, id, in.Code, in.Name, in.TimeZone, in.PlatformUniversityID)
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

// GetByPlatformUniversityID 按平台院校标识返回绑定校。
// 部分唯一索引保证至多一行；未绑定报 not_found。
func (r *SchoolRepo) GetByPlatformUniversityID(ctx context.Context, universityID string) (coreschool.Record, error) {
	row := r.db.q(ctx).QueryRow(ctx,
		`SELECT `+schoolColumns+` FROM schools WHERE platform_university_id = $1`, universityID)
	return recordOrNotFound(scanSchool(row))
}

// Update 修改 in 中出现的字段。nil 字段保持原值；
// PlatformUniversityID 指向空串清空绑定（落 NULL），指向值则覆盖。
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
		       platform_university_id = CASE
		           WHEN $5::text IS NULL THEN platform_university_id
		           ELSE NULLIF($5::text, '') END,
		       updated_at = now()
		 WHERE id = $1::uuid
		RETURNING `+schoolColumns, id, in.Name, status, in.TimeZone, in.PlatformUniversityID)
	return recordOrNotFound(scanSchool(row))
}

// List 按作用域列院校，游标分页。
// master 走无谓词语句；admin 走 id = ANY(...) 语句；
// user 只见 active 行（8v8①：建号表单的学校下拉数据源）。
// user 分支是本方法私有的：schoolScopePredicate 是 schedule 与
// photo 共用的谓词，本批不扩它的可见面。
// 排序键是 (created_at DESC, id DESC)。
func (r *SchoolRepo) List(ctx context.Context, s corescope.Scope, cursor string, limit int) ([]coreschool.Record, string, error) {
	if limit < 1 || limit > 200 {
		return nil, "", validationError("limit must be between 1 and 200")
	}
	var query string
	var args []any
	if s.Kind() == corescope.KindUser {
		query = `SELECT ` + schoolColumns + ` FROM schools WHERE status = 'active'`
	} else {
		clause, scopeArgs, err := schoolScopePredicate(s, 1, "")
		if err != nil {
			return nil, "", err
		}
		args = scopeArgs
		query = `SELECT ` + schoolColumns + ` FROM schools WHERE true` + clause
	}
	query, args, err := appendKeyset(query, args, cursor, "created_at", "id")
	if err != nil {
		return nil, "", err
	}
	query += ` ORDER BY created_at DESC, id DESC`
	query, args = appendLimit(query, args, limit)

	rows, err := r.db.q(ctx).Query(ctx, query, args...)
	if err != nil {
		return nil, "", mapError(err)
	}
	defer rows.Close()

	items := make([]coreschool.Record, 0, limit)
	for rows.Next() {
		record, err := scanSchool(rows)
		if err != nil {
			return nil, "", mapError(err)
		}
		items = append(items, record)
	}
	if err := rows.Err(); err != nil {
		return nil, "", mapError(err)
	}
	more := len(items) > limit
	if more {
		items = items[:limit]
	}
	next := nextCursor(items, more, func(rec coreschool.Record) (time.Time, string) {
		return rec.CreatedAt, rec.ID
	})
	return items, next, nil
}

func scanSchool(row pgx.Row) (coreschool.Record, error) {
	var (
		record coreschool.Record
		status string
	)
	if err := row.Scan(&record.ID, &record.Code, &record.Name, &status,
		&record.TimeZone, &record.PlatformUniversityID, &record.CreatedAt, &record.UpdatedAt); err != nil {
		return coreschool.Record{}, err
	}
	record.Status = coreschool.Status(status)
	return record, nil
}
