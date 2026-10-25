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

// PasswordResetRepo is the PostgreSQL one-time reset token repository.
type PasswordResetRepo struct {
	db *DB
}

// NewPasswordResetRepo builds a reset token repository.
func NewPasswordResetRepo(db *DB) *PasswordResetRepo { return &PasswordResetRepo{db: db} }

const resetColumns = `id::text, user_id::text, token_hash, created_at, expires_at, used_at`

// Create stores a reset token. Only the SHA-256 hash is stored; TokenHash is
// the typed digest, so a raw token cannot reach this call.
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
		// A reset token for a user that does not exist is a missing parent.
		return coreauth.PasswordReset{}, mapInsertForeignKey(err)
	}
	return record, nil
}

// Consume marks a token used and returns it.
//
// The UPDATE is the single-use decision: only a row that is unused and
// unexpired can change. A second call, an expired token and an unknown token
// all return not_found.
func (r *PasswordResetRepo) Consume(ctx context.Context, tokenHash coreauth.TokenHash, now time.Time) (coreauth.PasswordReset, error) {
	if tokenHash.IsZero() {
		return coreauth.PasswordReset{}, coreerrNotFound()
	}
	row := r.db.q(ctx).QueryRow(ctx, `
		UPDATE password_resets
		   SET used_at = $2
		 WHERE token_hash = $1
		   AND used_at IS NULL
		   AND expires_at > $2
		RETURNING `+resetColumns, tokenHash.Bytes(), now)
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
