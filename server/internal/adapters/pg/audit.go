package pg

import (
	"context"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"

	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
)

// AuditRepo is the PostgreSQL audit repository.
//
// It owns the only chain-append implementation. The key comes from
// configuration; an empty key is refused, so a misconfigured process cannot
// write an unkeyed chain.
type AuditRepo struct {
	db  *DB
	key []byte
}

// NewAuditRepo builds an audit repository. key is the HMAC key for the chain.
func NewAuditRepo(db *DB, key []byte) (*AuditRepo, error) {
	if len(key) == 0 {
		return nil, errors.New("pg: audit key is required")
	}
	return &AuditRepo{db: db, key: append([]byte(nil), key...)}, nil
}

// Append writes the next link of the chain.
//
// It must run inside a transaction. It takes AuditChainLock, reads the head,
// computes the hash and inserts, so two transactions cannot append the same
// predecessor. The row is inserted last: a long transaction holds the chain
// lock and blocks every audited write.
func (r *AuditRepo) Append(ctx context.Context, in coreaudit.Entry) (coreaudit.Row, error) {
	if !r.db.hasTx(ctx) {
		return coreaudit.Row{}, ErrNoTransaction
	}
	if in.TS.IsZero() {
		return coreaudit.Row{}, validationError("audit timestamp is required")
	}
	if in.Action == "" {
		return coreaudit.Row{}, validationError("audit action is required")
	}
	if in.ActorLabel == "" {
		return coreaudit.Row{}, validationError("audit actor label is required")
	}
	detail, err := coreaudit.EncodeDetail(in.Detail)
	if err != nil {
		return coreaudit.Row{}, validationError(err.Error())
	}

	q := r.db.q(ctx)
	if _, err := q.Exec(ctx, "SELECT pg_advisory_xact_lock($1)", AuditChainLock); err != nil {
		return coreaudit.Row{}, mapError(err)
	}

	prev := coreaudit.ZeroHash()
	var head []byte
	if err := q.QueryRow(ctx,
		`SELECT hash FROM audit_logs ORDER BY id DESC LIMIT 1`).Scan(&head); err != nil {
		if !errors.Is(err, pgx.ErrNoRows) {
			return coreaudit.Row{}, mapError(err)
		}
	} else {
		prev = head
	}

	row := coreaudit.Row{
		TS:          in.TS.UTC().Truncate(time.Microsecond),
		SchoolID:    in.SchoolID,
		ActorUserID: in.ActorUserID,
		ActorRole:   string(in.ActorRole),
		ActorLabel:  in.ActorLabel,
		Action:      string(in.Action),
		TargetType:  in.TargetType,
		TargetID:    in.TargetID,
		Detail:      detail,
		PrevHash:    prev,
	}
	row.Hash = coreaudit.Hash(r.key, row)

	if err := q.QueryRow(ctx, `
		INSERT INTO audit_logs
		       (ts, school_id, actor_user_id, actor_role, actor_label, action,
		        target_type, target_id, detail, prev_hash, hash)
		VALUES ($1, NULLIF($2, '')::uuid, NULLIF($3, '')::uuid, NULLIF($4, '')::user_role,
		        $5, $6, $7, $8, $9, $10, $11)
		RETURNING id`,
		row.TS, row.SchoolID, row.ActorUserID, row.ActorRole, row.ActorLabel,
		row.Action, row.TargetType, row.TargetID, row.Detail, row.PrevHash, row.Hash).Scan(&row.ID); err != nil {
		return coreaudit.Row{}, mapError(err)
	}
	return row, nil
}

// Verify checks the chain over an inclusive id range. Zero means unbounded on
// that side. An empty range is an error: a verification that looked at nothing
// is not a pass.
func (r *AuditRepo) Verify(ctx context.Context, from, to int64) (coreaudit.Report, error) {
	rows, err := r.db.q(ctx).Query(ctx, `
		SELECT id, ts, coalesce(school_id::text, ''), coalesce(actor_user_id::text, ''),
		       coalesce(actor_role::text, ''), actor_label, action, target_type,
		       target_id, detail, prev_hash, hash
		  FROM audit_logs
		 WHERE ($1 = 0 OR id >= $1)
		   AND ($2 = 0 OR id <= $2)
		 ORDER BY id`, from, to)
	if err != nil {
		return coreaudit.Report{}, mapError(err)
	}
	defer rows.Close()

	var entries []coreaudit.Row
	for rows.Next() {
		var entry coreaudit.Row
		if err := rows.Scan(&entry.ID, &entry.TS, &entry.SchoolID, &entry.ActorUserID,
			&entry.ActorRole, &entry.ActorLabel, &entry.Action, &entry.TargetType,
			&entry.TargetID, &entry.Detail, &entry.PrevHash, &entry.Hash); err != nil {
			return coreaudit.Report{}, mapError(err)
		}
		entries = append(entries, entry)
	}
	if err := rows.Err(); err != nil {
		return coreaudit.Report{}, mapError(err)
	}
	return coreaudit.Verify(r.key, entries)
}
