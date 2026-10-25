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

// SessionRepo is the PostgreSQL session repository.
type SessionRepo struct {
	db *DB
}

// NewSessionRepo builds a session repository.
func NewSessionRepo(db *DB) *SessionRepo { return &SessionRepo{db: db} }

const sessionColumns = `id::text, user_id::text, token_hash, auth_method,
	created_at, last_seen_at, idle_expires_at, expires_at, revoked_at,
	coalesce(ip_hash, ''), coalesce(user_agent_hash, '')`

// Create stores a session and evicts the oldest live sessions beyond
// coreauth.MaxSessionsPerUser. Insert and eviction share one transaction, so a
// concurrent reader never observes more than the cap for long.
func (r *SessionRepo) Create(ctx context.Context, in coreauth.NewSessionRow) (coreauth.SessionRow, error) {
	if in.TokenHash.IsZero() {
		return coreauth.SessionRow{}, validationError("token hash is required")
	}
	if in.AuthMethod != "password" && in.AuthMethod != "reset" {
		return coreauth.SessionRow{}, validationError("unknown auth method")
	}
	if in.Now.IsZero() || in.ExpiresAt.IsZero() || in.IdleExpiresAt.IsZero() {
		return coreauth.SessionRow{}, validationError("session times are required")
	}
	if in.IdleExpiresAt.After(in.ExpiresAt) {
		return coreauth.SessionRow{}, validationError("idle expiry must not exceed absolute expiry")
	}
	sessionID, err := newID()
	if err != nil {
		return coreauth.SessionRow{}, err
	}

	var record coreauth.SessionRow
	err = r.db.InTx(ctx, func(ctx context.Context) error {
		q := r.db.q(ctx)
		row := q.QueryRow(ctx, `
			INSERT INTO sessions
			       (id, user_id, token_hash, auth_method, created_at, last_seen_at,
			        idle_expires_at, expires_at, ip_hash, user_agent_hash)
			VALUES ($1::uuid, $2::uuid, $3, $4, $5, $5, $6, $7, NULLIF($8, ''), NULLIF($9, ''))
			RETURNING `+sessionColumns,
			sessionID, in.UserID, in.TokenHash.Bytes(), in.AuthMethod, in.Now,
			in.IdleExpiresAt, in.ExpiresAt, string(in.IPHash), string(in.UserAgentHash))
		var scanErr error
		record, scanErr = scanSession(row)
		if scanErr != nil {
			return scanErr
		}
		_, err := q.Exec(ctx, `
			DELETE FROM sessions
			 WHERE user_id = $1::uuid
			   AND revoked_at IS NULL
			   AND id IN (
			       SELECT id FROM sessions
			        WHERE user_id = $1::uuid AND revoked_at IS NULL
			        ORDER BY last_seen_at DESC, id DESC
			        OFFSET $2)`, in.UserID, coreauth.MaxSessionsPerUser)
		return err
	})
	if err != nil {
		return coreauth.SessionRow{}, mapError(err)
	}
	return record, nil
}

// Resolve returns a live session joined with its user and refreshes the idle
// window. Unknown, revoked, expired and idle sessions all return not_found.
// A disabled or deleted user makes the session unresolvable without an
// explicit revoke.
//
// The refresh moves both last_seen_at and idle_expires_at, so the returned row
// is self-consistent: a caller that sets a cookie Max-Age from IdleExpiresAt
// gets the same instant the next request will check. The idle expiry is capped
// by the absolute expiry, so the check constraint idle_expires_at <=
// expires_at always holds.
func (r *SessionRepo) Resolve(ctx context.Context, tokenHash coreauth.TokenHash, now time.Time, idle time.Duration) (coreauth.ResolvedSession, error) {
	if tokenHash.IsZero() {
		return coreauth.ResolvedSession{}, coreerrs.ErrNotFound
	}
	if idle <= 0 {
		return coreauth.ResolvedSession{}, validationError("idle window must be positive")
	}
	cutoff := now.Add(-idle)
	row := r.db.q(ctx).QueryRow(ctx, `
		WITH refreshed AS (
		    UPDATE sessions
		       SET last_seen_at = $2,
		           idle_expires_at = LEAST($2 + (idle_expires_at - last_seen_at), expires_at)
		     WHERE token_hash = $1
		       AND revoked_at IS NULL
		       AND expires_at > $2
		       AND last_seen_at > $3
		    RETURNING id, user_id, token_hash, auth_method, created_at, last_seen_at,
		              idle_expires_at, expires_at, revoked_at, ip_hash, user_agent_hash
		)
		SELECT f.id::text, f.user_id::text, f.token_hash, f.auth_method, f.created_at,
		       f.last_seen_at, f.idle_expires_at, f.expires_at, f.revoked_at,
		       coalesce(f.ip_hash, ''), coalesce(f.user_agent_hash, ''),
		       u.email, u.role::text, u.status::text, u.must_change_password
		  FROM refreshed f
		  JOIN users u ON u.id = f.user_id
		 WHERE u.deleted_at IS NULL AND u.status = 'active'`,
		tokenHash.Bytes(), now, cutoff)
	return scanResolvedSession(row)
}

// Revoke marks one session revoked. It is idempotent.
func (r *SessionRepo) Revoke(ctx context.Context, tokenHash coreauth.TokenHash, now time.Time) error {
	if tokenHash.IsZero() {
		return nil
	}
	_, err := r.db.q(ctx).Exec(ctx, `
		UPDATE sessions SET revoked_at = $2
		 WHERE token_hash = $1 AND revoked_at IS NULL`, tokenHash.Bytes(), now)
	return mapError(err)
}

// RevokeAllForUser revokes every live session of userID and reports how many
// rows changed. It is idempotent.
func (r *SessionRepo) RevokeAllForUser(ctx context.Context, userID string, now time.Time) (int64, error) {
	tag, err := r.db.q(ctx).Exec(ctx, `
		UPDATE sessions SET revoked_at = $2
		 WHERE user_id = $1::uuid AND revoked_at IS NULL`, userID, now)
	if err != nil {
		return 0, mapError(err)
	}
	return tag.RowsAffected(), nil
}

// DeleteExpired removes at most limit sessions that expired before the given
// time. Cleanup runs in batches so it never holds a long lock.
func (r *SessionRepo) DeleteExpired(ctx context.Context, before time.Time, limit int) (int64, error) {
	if limit <= 0 {
		return 0, validationError("limit must be positive")
	}
	tag, err := r.db.q(ctx).Exec(ctx, `
		DELETE FROM sessions
		 WHERE id IN (
		     SELECT id FROM sessions WHERE expires_at < $1
		      ORDER BY expires_at ASC LIMIT $2)`, before, limit)
	if err != nil {
		return 0, mapError(err)
	}
	return tag.RowsAffected(), nil
}

func scanSession(row pgx.Row) (coreauth.SessionRow, error) {
	var (
		record  coreauth.SessionRow
		rawHash []byte
		revoked pgtype.Timestamptz
	)
	if err := row.Scan(&record.ID, &record.UserID, &rawHash, &record.AuthMethod,
		&record.CreatedAt, &record.LastSeenAt, &record.IdleExpiresAt, &record.ExpiresAt,
		&revoked, &record.IPHash, &record.UserAgentHash); err != nil {
		return coreauth.SessionRow{}, err
	}
	hash, err := tokenHashFromBytes(rawHash)
	if err != nil {
		return coreauth.SessionRow{}, err
	}
	record.TokenHash = hash
	record.RevokedAt = timePtr(revoked)
	return record, nil
}

func scanResolvedSession(row pgx.Row) (coreauth.ResolvedSession, error) {
	var (
		record  coreauth.ResolvedSession
		rawHash []byte
		revoked pgtype.Timestamptz
		role    string
		status  string
	)
	err := row.Scan(&record.Session.ID, &record.Session.UserID, &rawHash,
		&record.Session.AuthMethod, &record.Session.CreatedAt, &record.Session.LastSeenAt,
		&record.Session.IdleExpiresAt, &record.Session.ExpiresAt, &revoked,
		&record.Session.IPHash, &record.Session.UserAgentHash,
		&record.User.Email, &role, &status, &record.User.MustChangePassword)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return coreauth.ResolvedSession{}, coreerrs.ErrNotFound
		}
		return coreauth.ResolvedSession{}, mapError(err)
	}
	hash, err := tokenHashFromBytes(rawHash)
	if err != nil {
		return coreauth.ResolvedSession{}, err
	}
	record.Session.TokenHash = hash
	record.Session.RevokedAt = timePtr(revoked)
	record.User.ID = record.Session.UserID
	record.User.Role = coreauth.Role(role)
	record.User.Status = coreauth.Status(status)
	return record, nil
}
