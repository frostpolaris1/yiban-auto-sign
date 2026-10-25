package pg

import (
	"context"
	"errors"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// UserRepo 是 PostgreSQL 用户仓储。
type UserRepo struct {
	db *DB
}

// NewUserRepo 构造用户仓储。
func NewUserRepo(db *DB) *UserRepo { return &UserRepo{db: db} }

const userColumns = `id::text, email, password_hash, role::text, status::text,
	must_change_password, password_changed_at, last_login_at, created_at,
	updated_at, deleted_at, coalesce(deleted_by::text, '')`

// Create 在活跃用户数低于 limit 时插入用户。
//
// **必须在事务内运行**。它按序取 UserCapacityLock、统计活跃行、插入，
// 三者同属一个事务且处于 READ COMMITTED。
// 绕过本函数直接 INSERT 不受该锁保护（schema §6.6）。
func (r *UserRepo) Create(ctx context.Context, in coreuser.New, limit int) (coreuser.Record, error) {
	if !r.db.hasTx(ctx) {
		return coreuser.Record{}, ErrNoTransaction
	}
	if limit <= 0 {
		return coreuser.Record{}, validationError("limit must be positive")
	}
	email := strings.ToLower(strings.TrimSpace(in.Email))
	if email == "" {
		return coreuser.Record{}, validationError("email is required")
	}
	if in.PasswordHash == "" {
		return coreuser.Record{}, validationError("password hash is required")
	}
	role := in.Role
	if role == "" {
		role = coreauth.RoleUser
	}
	if !role.Valid() {
		return coreuser.Record{}, validationError("unknown role")
	}
	id := in.ID
	if id == "" {
		var err error
		id, err = newID()
		if err != nil {
			return coreuser.Record{}, err
		}
	}

	q := r.db.q(ctx)
	if _, err := q.Exec(ctx, "SELECT pg_advisory_xact_lock($1)", UserCapacityLock); err != nil {
		return coreuser.Record{}, mapError(err)
	}
	row := q.QueryRow(ctx, `
		WITH live AS (SELECT count(*) AS n FROM users WHERE deleted_at IS NULL)
		INSERT INTO users (id, email, password_hash, role, must_change_password)
		SELECT $1::uuid, $2, $3, $4::user_role, $5
		  FROM live
		 WHERE live.n < $6
		RETURNING `+userColumns,
		id, email, in.PasswordHash, string(role), in.MustChangePassword, limit)
	record, err := scanUser(row)
	if errors.Is(err, pgx.ErrNoRows) {
		return coreuser.Record{}, capacityError()
	}
	return record, mapError(err)
}

// GetByID 返回一个未删除用户。
func (r *UserRepo) GetByID(ctx context.Context, id string) (coreuser.Record, error) {
	row := r.db.q(ctx).QueryRow(ctx,
		`SELECT `+userColumns+` FROM users WHERE id = $1::uuid AND deleted_at IS NULL`, id)
	return recordOrNotFound(scanUser(row))
}

// GetByEmail 按登录名返回未删除用户。
// 入参与 Create 做同样的归一（小写、去空白）。
func (r *UserRepo) GetByEmail(ctx context.Context, email string) (coreuser.Record, error) {
	row := r.db.q(ctx).QueryRow(ctx,
		`SELECT `+userColumns+` FROM users WHERE email = $1 AND deleted_at IS NULL`,
		strings.ToLower(strings.TrimSpace(email)))
	return recordOrNotFound(scanUser(row))
}

// UpdatePassword 替换密码哈希与改密标记。
func (r *UserRepo) UpdatePassword(ctx context.Context, id, passwordHash string, mustChange bool, at time.Time) error {
	if passwordHash == "" {
		return validationError("password hash is required")
	}
	tag, err := r.db.q(ctx).Exec(ctx, `
		UPDATE users
		   SET password_hash = $2, must_change_password = $3,
		       password_changed_at = $4, updated_at = now()
		 WHERE id = $1::uuid AND deleted_at IS NULL`,
		id, passwordHash, mustChange, at)
	if err != nil {
		return mapError(err)
	}
	if tag.RowsAffected() == 0 {
		return coreerrNotFound()
	}
	return nil
}

// RehashPassword 只替换口令哈希。
//
// 登录时的透明升级走本方法：口令没有变，故 password_changed_at
// 与 must_change_password **保持不变**。改密请用 UpdatePassword。
func (r *UserRepo) RehashPassword(ctx context.Context, id, passwordHash string) error {
	if passwordHash == "" {
		return validationError("password hash is required")
	}
	tag, err := r.db.q(ctx).Exec(ctx, `
		UPDATE users SET password_hash = $2, updated_at = now()
		 WHERE id = $1::uuid AND deleted_at IS NULL`, id, passwordHash)
	if err != nil {
		return mapError(err)
	}
	if tag.RowsAffected() == 0 {
		return coreerrNotFound()
	}
	return nil
}

// SetStatus 修改用户状态。
// 停用用户**还必须撤销其会话**；那属于调用方的事务，不在本方法内。
func (r *UserRepo) SetStatus(ctx context.Context, id string, status coreauth.Status, at time.Time) error {
	if !status.Valid() {
		return validationError("unknown user status")
	}
	tag, err := r.db.q(ctx).Exec(ctx, `
		UPDATE users SET status = $2::user_status, updated_at = $3
		 WHERE id = $1::uuid AND deleted_at IS NULL`,
		id, string(status), at)
	if err != nil {
		return mapError(err)
	}
	if tag.RowsAffected() == 0 {
		return coreerrNotFound()
	}
	return nil
}

// MarkLogin 记录一次成功登录的时间。
func (r *UserRepo) MarkLogin(ctx context.Context, id string, at time.Time) error {
	tag, err := r.db.q(ctx).Exec(ctx, `
		UPDATE users SET last_login_at = $2, updated_at = now()
		 WHERE id = $1::uuid AND deleted_at IS NULL`, id, at)
	if err != nil {
		return mapError(err)
	}
	if tag.RowsAffected() == 0 {
		return coreerrNotFound()
	}
	return nil
}

// SoftDelete 把用户标记为已删除。
// 物理删除留给后续清理任务；用户名下的账号未清空之前，删除会被拦截。
func (r *UserRepo) SoftDelete(ctx context.Context, id, by string, at time.Time) error {
	if strings.TrimSpace(by) == "" {
		return validationError("deleted_by is required")
	}
	tag, err := r.db.q(ctx).Exec(ctx, `
		UPDATE users
		   SET deleted_at = $2, deleted_by = $3::uuid, updated_at = now()
		 WHERE id = $1::uuid AND deleted_at IS NULL`, id, at, by)
	if err != nil {
		return mapError(err)
	}
	if tag.RowsAffected() == 0 {
		return coreerrNotFound()
	}
	return nil
}

func scanUser(row pgx.Row) (coreuser.Record, error) {
	var (
		record          coreuser.Record
		role, status    string
		passwordChanged pgtype.Timestamptz
		lastLogin       pgtype.Timestamptz
		deleted         pgtype.Timestamptz
	)
	if err := row.Scan(&record.ID, &record.Email, &record.PasswordHash, &role, &status,
		&record.MustChangePassword, &passwordChanged, &lastLogin, &record.CreatedAt,
		&record.UpdatedAt, &deleted, &record.DeletedBy); err != nil {
		return coreuser.Record{}, err
	}
	record.Role = coreauth.Role(role)
	record.Status = coreauth.Status(status)
	record.PasswordChangedAt = timePtr(passwordChanged)
	record.LastLoginAt = timePtr(lastLogin)
	record.DeletedAt = timePtr(deleted)
	return record, nil
}
