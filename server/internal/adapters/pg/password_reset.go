package pg

import (
	"context"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

// PasswordResetRepo 是 PostgreSQL 一次性重置令牌仓储。
type PasswordResetRepo struct {
	db *DB
}

// NewPasswordResetRepo 构造重置令牌仓储。
func NewPasswordResetRepo(db *DB) *PasswordResetRepo { return &PasswordResetRepo{db: db} }

const resetColumns = `id::text, user_id::text, token_hash, created_at, expires_at, used_at`

// Create 存储一个重置令牌。**只存 SHA-256 摘要**；
// TokenHash 是定型摘要，原始令牌无法传到本调用。
func (r *PasswordResetRepo) Create(ctx context.Context, in coreauth.NewPasswordReset) (coreauth.PasswordReset, error) {
	if in.TokenHash.IsZero() {
		return coreauth.PasswordReset{}, validationError("token hash is required")
	}
	if in.Now.IsZero() || !in.ExpiresAt.After(in.Now) {
		return coreauth.PasswordReset{}, validationError("expiry must be after creation")
	}
	id, err := newID()
	if err != nil {
		return coreauth.PasswordReset{}, err
	}
	row := r.db.q(ctx).QueryRow(ctx, `
		INSERT INTO password_resets (id, user_id, token_hash, created_at, expires_at)
		VALUES ($1::uuid, $2::uuid, $3, $4, $5)
		RETURNING `+resetColumns,
		id, in.UserID, in.TokenHash.Bytes(), in.Now, in.ExpiresAt)
	record, err := scanReset(row)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return coreauth.PasswordReset{}, coreerrs.ErrNotFound
		}
		// 给不存在的用户发重置令牌，属于父行缺失。
		return coreauth.PasswordReset{}, mapInsertForeignKey(err)
	}
	return record, nil
}

// Peek 读一个未使用、未过期的令牌行，不改变状态。
// 用例在付出口令哈希之前先用它判令牌；真正的单次使用判据仍是 Consume。
func (r *PasswordResetRepo) Peek(ctx context.Context, tokenHash coreauth.TokenHash, now time.Time) (coreauth.PasswordReset, error) {
	if tokenHash.IsZero() {
		return coreauth.PasswordReset{}, coreerrNotFound()
	}
	row := r.db.q(ctx).QueryRow(ctx, `
		SELECT `+resetColumns+` FROM password_resets
		 WHERE token_hash = $1
		   AND used_at IS NULL
		   AND expires_at > $2`, tokenHash.Bytes(), now)
	record, err := scanReset(row)
	return recordOrNotFound(record, err)
}

// Consume 把令牌标记为已用，并失效同用户其余未用且未过期的令牌，
// 返回被消费的行。
//
// 这条 UPDATE 就是**单次使用判据**：只有未用且未过期的目标令牌会命中；
// 同一语句同时把该用户其余 used_at IS NULL 且未过期的行一并置位。
// 已过期的兄弟令牌本就不可用，**不置位也不加锁**：保留期清理按
// expires_at 升序删过期行，本语句若锁过期行会形成反向锁序而死锁。
// 第二次调用、已过期令牌、未知令牌一律 not_found。
//
// 一条语句完成两件事，故并发消费同一用户的不同令牌时行锁顺序一致，
// 不会互相死锁；后到的事务在锁释放后重判 used_at，返回零行即 not_found。
func (r *PasswordResetRepo) Consume(ctx context.Context, tokenHash coreauth.TokenHash, now time.Time) (coreauth.PasswordReset, error) {
	if tokenHash.IsZero() {
		return coreauth.PasswordReset{}, coreerrNotFound()
	}
	row := r.db.q(ctx).QueryRow(ctx, `
		WITH target AS (
			SELECT user_id FROM password_resets
			 WHERE token_hash = $1
			   AND used_at IS NULL
			   AND expires_at > $2
		), updated AS (
			UPDATE password_resets
			   SET used_at = $2
			 WHERE user_id = (SELECT user_id FROM target)
			   AND used_at IS NULL
			   AND expires_at > $2
			RETURNING `+resetColumns+`
		)
		SELECT `+resetColumns+` FROM updated WHERE token_hash = $1`,
		tokenHash.Bytes(), now)
	record, err := scanReset(row)
	return recordOrNotFound(record, err)
}

func scanReset(row pgx.Row) (coreauth.PasswordReset, error) {
	var (
		record  coreauth.PasswordReset
		rawHash []byte
		usedAt  pgtype.Timestamptz
	)
	if err := row.Scan(&record.ID, &record.UserID, &rawHash,
		&record.CreatedAt, &record.ExpiresAt, &usedAt); err != nil {
		return coreauth.PasswordReset{}, err
	}
	hash, err := tokenHashFromBytes(rawHash)
	if err != nil {
		return coreauth.PasswordReset{}, err
	}
	record.TokenHash = hash
	record.UsedAt = timePtr(usedAt)
	return record, nil
}
