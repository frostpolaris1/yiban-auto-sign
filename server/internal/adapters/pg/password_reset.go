package pg

import (
	"context"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

// PasswordResetRepo is the PostgreSQL one-time reset token repository.
type PasswordResetRepo struct {
	db *DB
}

// NewPasswordResetRepo builds a reset token repository.
func NewPasswordResetRepo(db *DB) *PasswordResetRepo { return &PasswordResetRepo{db: db} }

const resetColumns = `id::text, user_id::text, token_hash, created_at, expires_at, used_at`

// Create stores a reset token. Only the SHA-256 hash is stored.
func (r *PasswordResetRepo) Create(ctx context.Context, in coreauth.NewPasswordReset) (coreauth.PasswordReset, error) {
	if len(in.TokenHash) != 32 {
		return coreauth.PasswordReset{}, validationError("token hash must be 32 bytes")
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
		id, in.UserID, in.TokenHash, in.Now, in.ExpiresAt)
	record, err := scanReset(row)
	return recordOrNotFound(record, err)
}

// Consume marks a token used and returns it.
//
// The UPDATE is the single-use decision: only a row that is unused and
// unexpired can change. A second call, an expired token and an unknown token
// all return not_found.
func (r *PasswordResetRepo) Consume(ctx context.Context, tokenHash []byte, now time.Time) (coreauth.PasswordReset, error) {
	if len(tokenHash) != 32 {
		return coreauth.PasswordReset{}, coreerrNotFound()
	}
	row := r.db.q(ctx).QueryRow(ctx, `
		UPDATE password_resets
		   SET used_at = $2
		 WHERE token_hash = $1
		   AND used_at IS NULL
		   AND expires_at > $2
		RETURNING `+resetColumns, tokenHash, now)
	record, err := scanReset(row)
	return recordOrNotFound(record, err)
}

func scanReset(row pgx.Row) (coreauth.PasswordReset, error) {
	var (
		record coreauth.PasswordReset
		usedAt pgtype.Timestamptz
	)
	if err := row.Scan(&record.ID, &record.UserID, &record.TokenHash,
		&record.CreatedAt, &record.ExpiresAt, &usedAt); err != nil {
		return coreauth.PasswordReset{}, err
	}
	record.UsedAt = timePtr(usedAt)
	return record, nil
}
