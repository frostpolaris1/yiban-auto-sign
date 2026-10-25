package pg

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// AccountRepo is the PostgreSQL account repository.
type AccountRepo struct {
	db *DB
}

// NewAccountRepo builds an account repository.
func NewAccountRepo(db *DB) *AccountRepo { return &AccountRepo{db: db} }

const accountColumns = `id::text, school_id::text, owner_user_id::text, phone,
	status::text, reject_reason, reviewed_at, coalesce(reviewed_by::text, ''),
	created_at, updated_at, deleted_at, coalesce(deleted_by::text, '')`

const credentialColumns = `c.account_id::text, c.school_id::text, c.alg, c.kid,
	c.aad_version, c.nonce, c.ciphertext, c.version, c.created_at, c.updated_at,
	c.rekeyed_at, c.last_verified_at`

// Create inserts an account when the live-account count is below limit.
//
// It must run inside a transaction: the advisory lock, the conditional insert
// and any credential/audit rows belong to one unit. The conditional INSERT
// returns zero rows at the cap, which maps to capacity_exceeded.
func (r *AccountRepo) Create(ctx context.Context, in coreaccount.New, limit int) (coreaccount.Record, error) {
	if !r.db.hasTx(ctx) {
		return coreaccount.Record{}, ErrNoTransaction
	}
	if limit <= 0 {
		return coreaccount.Record{}, validationError("limit must be positive")
	}
	if strings.TrimSpace(in.SchoolID) == "" || strings.TrimSpace(in.OwnerUserID) == "" || strings.TrimSpace(in.Phone) == "" {
		return coreaccount.Record{}, validationError("school, owner and phone are required")
	}
	id := in.ID
	if id == "" {
		var err error
		id, err = newID()
		if err != nil {
			return coreaccount.Record{}, err
		}
	}

	q := r.db.q(ctx)
	if _, err := q.Exec(ctx, "SELECT pg_advisory_xact_lock($1)", AccountCapacityLock); err != nil {
		return coreaccount.Record{}, mapError(err)
	}
	row := q.QueryRow(ctx, `
		WITH live AS (SELECT count(*) AS n FROM accounts WHERE deleted_at IS NULL)
		INSERT INTO accounts (id, school_id, owner_user_id, phone)
		SELECT $1::uuid, $2::uuid, $3::uuid, $4
		  FROM live
		 WHERE live.n < $5
		RETURNING `+accountColumns,
		id, in.SchoolID, in.OwnerUserID, in.Phone, limit)
	record, err := scanAccount(row)
	if errors.Is(err, pgx.ErrNoRows) {
		return coreaccount.Record{}, capacityError()
	}
	return record, mapError(err)
}

// Get returns one live account inside scope.
func (r *AccountRepo) Get(ctx context.Context, s corescope.Scope, id string) (coreaccount.Record, error) {
	return r.fetch(ctx, s, id, false)
}

// SetStatus changes the status only when the row is currently from.
func (r *AccountRepo) SetStatus(ctx context.Context, s corescope.Scope, id string, from, to coreaccount.Status, at time.Time, reason string) (coreaccount.Record, error) {
	if !from.Valid() || !to.Valid() {
		return coreaccount.Record{}, validationError("unknown account status")
	}
	if to == coreaccount.StatusRejected && strings.TrimSpace(reason) == "" {
		return coreaccount.Record{}, validationError("reject reason is required")
	}
	scopeSQL, scopeArgs, err := scopePredicate(s, 2, "")
	if err != nil {
		return coreaccount.Record{}, err
	}
	args := append([]any{id}, scopeArgs...)
	next := 2 + len(scopeArgs)
	query := fmt.Sprintf(`
		UPDATE accounts
		   SET status = $%d::account_status,
		       reject_reason = CASE WHEN $%d = 'rejected' THEN $%d ELSE reject_reason END,
		       updated_at = $%d
		 WHERE id = $1::uuid
		   AND status = $%d::account_status
		   AND deleted_at IS NULL%s
		RETURNING %s`,
		next, next, next+1, next+2, next+3, scopeSQL, accountColumns)
	args = append(args, string(to), reason, at, string(from))
	row := r.db.q(ctx).QueryRow(ctx, query, args...)
	record, err := scanAccount(row)
	if errors.Is(err, pgx.ErrNoRows) {
		return r.classifyMiss(ctx, s, id)
	}
	return record, mapError(err)
}

// SoftDelete marks one account deleted. The row stays for 30 days so a
// mistaken delete can be restored; the credential row stays with it.
func (r *AccountRepo) SoftDelete(ctx context.Context, s corescope.Scope, id, by string, at time.Time) error {
	if strings.TrimSpace(by) == "" {
		return validationError("deleted_by is required")
	}
	scopeSQL, scopeArgs, err := scopePredicate(s, 4, "")
	if err != nil {
		return err
	}
	args := append([]any{id, at, by}, scopeArgs...)
	tag, err := r.db.q(ctx).Exec(ctx, `
		UPDATE accounts
		   SET deleted_at = $2, deleted_by = $3::uuid, updated_at = now()
		 WHERE id = $1::uuid AND deleted_at IS NULL`+scopeSQL, args...)
	if err != nil {
		return mapError(err)
	}
	if tag.RowsAffected() == 0 {
		return coreerrs.ErrNotFound
	}
	return nil
}

// Restore clears the soft-delete mark when a capacity slot is free.
//
// It must run inside a transaction. Soft-deleted rows do not count toward the
// limit, so a restore must recount under the lock; counting earlier would let
// two concurrent restores push the total past the limit.
func (r *AccountRepo) Restore(ctx context.Context, s corescope.Scope, id string, limit int) (coreaccount.Record, error) {
	if !r.db.hasTx(ctx) {
		return coreaccount.Record{}, ErrNoTransaction
	}
	if limit <= 0 {
		return coreaccount.Record{}, validationError("limit must be positive")
	}
	if _, err := r.db.q(ctx).Exec(ctx, "SELECT pg_advisory_xact_lock($1)", AccountCapacityLock); err != nil {
		return coreaccount.Record{}, mapError(err)
	}
	scopeSQL, scopeArgs, err := scopePredicate(s, 3, "")
	if err != nil {
		return coreaccount.Record{}, err
	}
	args := append([]any{id, limit}, scopeArgs...)
	row := r.db.q(ctx).QueryRow(ctx, `
		UPDATE accounts
		   SET deleted_at = NULL, deleted_by = NULL, updated_at = now()
		 WHERE id = $1::uuid
		   AND deleted_at IS NOT NULL
		   AND (SELECT count(*) FROM accounts WHERE deleted_at IS NULL) < $2`+scopeSQL+`
		RETURNING `+accountColumns, args...)
	record, err := scanAccount(row)
	if err == nil {
		return record, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return coreaccount.Record{}, mapError(err)
	}
	return r.classifyRestoreMiss(ctx, s, id)
}

// Credential returns the credential row of one account inside scope.
func (r *AccountRepo) Credential(ctx context.Context, s corescope.Scope, accountID string) (coreaccount.Credential, error) {
	scopeSQL, scopeArgs, err := scopePredicate(s, 2, "a.")
	if err != nil {
		return coreaccount.Credential{}, err
	}
	args := append([]any{accountID}, scopeArgs...)
	row := r.db.q(ctx).QueryRow(ctx, `
		SELECT `+credentialColumns+`
		  FROM credentials c
		  JOIN accounts a ON a.id = c.account_id
		 WHERE c.account_id = $1::uuid AND a.deleted_at IS NULL`+scopeSQL, args...)
	record, err := scanCredential(row)
	return recordOrNotFound(record, err)
}

// InsertCredential stores the first credential of an account. The composite
// foreign key rejects a school that differs from the account school.
func (r *AccountRepo) InsertCredential(ctx context.Context, in coreaccount.Credential) error {
	if strings.TrimSpace(in.AccountID) == "" || strings.TrimSpace(in.SchoolID) == "" {
		return validationError("account and school are required")
	}
	_, err := r.db.q(ctx).Exec(ctx, `
		INSERT INTO credentials
		       (account_id, school_id, alg, kid, aad_version, nonce, ciphertext, version)
		VALUES ($1::uuid, $2::uuid, COALESCE(NULLIF($3, ''), 'AES-256-GCM'), $4,
		        COALESCE($5, 1), $6, $7, COALESCE($8, 1))`,
		in.AccountID, in.SchoolID, in.Alg, in.KID, in.AADVersion,
		in.Nonce, in.Ciphertext, in.Version)
	return mapError(err)
}

// ReplaceCredential overwrites a credential using optimistic locking. A
// version mismatch changes nothing and reports conflict.
func (r *AccountRepo) ReplaceCredential(ctx context.Context, in coreaccount.Credential, expectedVersion int32) error {
	if expectedVersion < 1 {
		return validationError("expected version must be positive")
	}
	tag, err := r.db.q(ctx).Exec(ctx, `
		UPDATE credentials
		   SET alg = COALESCE(NULLIF($3, ''), alg),
		       kid = $4,
		       aad_version = COALESCE($5, aad_version),
		       nonce = $6,
		       ciphertext = $7,
		       version = $8,
		       rekeyed_at = COALESCE($9, rekeyed_at),
		       last_verified_at = COALESCE($10, last_verified_at),
		       updated_at = now()
		 WHERE account_id = $1::uuid AND school_id = $2::uuid AND version = $11`,
		in.AccountID, in.SchoolID, in.Alg, in.KID, in.AADVersion,
		in.Nonce, in.Ciphertext, in.Version, in.RekeyedAt, in.LastVerifiedAt,
		expectedVersion)
	if err != nil {
		return mapError(err)
	}
	if tag.RowsAffected() == 0 {
		return conflictError("credential version changed")
	}
	return nil
}

// fetch reads one account with or without the soft-delete filter, inside
// scope.
func (r *AccountRepo) fetch(ctx context.Context, s corescope.Scope, id string, includeDeleted bool) (coreaccount.Record, error) {
	scopeSQL, scopeArgs, err := scopePredicate(s, 2, "")
	if err != nil {
		return coreaccount.Record{}, err
	}
	args := append([]any{id}, scopeArgs...)
	deletedSQL := " AND deleted_at IS NULL"
	if includeDeleted {
		deletedSQL = ""
	}
	row := r.db.q(ctx).QueryRow(ctx,
		`SELECT `+accountColumns+` FROM accounts WHERE id = $1::uuid`+deletedSQL+scopeSQL, args...)
	record, err := scanAccount(row)
	return recordOrNotFound(record, err)
}

// classifyMiss explains a zero-row compare-and-set: a hidden row is
// not_found, a visible row with another status is a conflict.
func (r *AccountRepo) classifyMiss(ctx context.Context, s corescope.Scope, id string) (coreaccount.Record, error) {
	if _, err := r.fetch(ctx, s, id, false); err != nil {
		return coreaccount.Record{}, err
	}
	return coreaccount.Record{}, conflictError("account status changed")
}

// classifyRestoreMiss explains a zero-row restore.
func (r *AccountRepo) classifyRestoreMiss(ctx context.Context, s corescope.Scope, id string) (coreaccount.Record, error) {
	record, err := r.fetch(ctx, s, id, true)
	if err != nil {
		return coreaccount.Record{}, err
	}
	if record.DeletedAt == nil {
		return coreaccount.Record{}, conflictError("account is not deleted")
	}
	return coreaccount.Record{}, capacityError()
}

func scanAccount(row pgx.Row) (coreaccount.Record, error) {
	var (
		record     coreaccount.Record
		status     string
		reviewedAt pgtype.Timestamptz
		deletedAt  pgtype.Timestamptz
	)
	if err := row.Scan(&record.ID, &record.SchoolID, &record.OwnerUserID, &record.Phone,
		&status, &record.RejectReason, &reviewedAt, &record.ReviewedBy,
		&record.CreatedAt, &record.UpdatedAt, &deletedAt, &record.DeletedBy); err != nil {
		return coreaccount.Record{}, err
	}
	record.Status = coreaccount.Status(status)
	record.ReviewedAt = timePtr(reviewedAt)
	record.DeletedAt = timePtr(deletedAt)
	return record, nil
}

func scanCredential(row pgx.Row) (coreaccount.Credential, error) {
	var (
		record     coreaccount.Credential
		rekeyedAt  pgtype.Timestamptz
		verifiedAt pgtype.Timestamptz
	)
	if err := row.Scan(&record.AccountID, &record.SchoolID, &record.Alg, &record.KID,
		&record.AADVersion, &record.Nonce, &record.Ciphertext, &record.Version,
		&record.CreatedAt, &record.UpdatedAt, &rekeyedAt, &verifiedAt); err != nil {
		return coreaccount.Credential{}, err
	}
	record.RekeyedAt = timePtr(rekeyedAt)
	record.LastVerifiedAt = timePtr(verifiedAt)
	return record, nil
}
