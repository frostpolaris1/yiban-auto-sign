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

// AccountRepo 是 PostgreSQL 账号仓储。
type AccountRepo struct {
	db *DB
}

// NewAccountRepo 构造账号仓储。
func NewAccountRepo(db *DB) *AccountRepo { return &AccountRepo{db: db} }

const accountColumns = `id::text, school_id::text, owner_user_id::text, phone,
	status::text, reject_reason, reviewed_at, coalesce(reviewed_by::text, ''),
	created_at, updated_at, deleted_at, coalesce(deleted_by::text, '')`

const credentialColumns = `c.account_id::text, c.school_id::text, c.alg, c.kid,
	c.aad_version, c.nonce, c.ciphertext, c.version, c.created_at, c.updated_at,
	c.rekeyed_at, c.last_verified_at`

// Create 在活跃账号数低于 limit 时插入账号。
//
// **必须在事务内运行**：advisory lock、条件插入与凭据/审计行
// 同属一个工作单元。达到上限时条件 INSERT 返回零行，
// 映射为 capacity_exceeded。
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
	// 院校或属主缺失属父行缺失，不是冲突（§2.4）。
	return record, mapInsertForeignKey(err)
}

// Get 在范围内返回一个未删除账号。
func (r *AccountRepo) Get(ctx context.Context, s corescope.Scope, id string) (coreaccount.Record, error) {
	return r.fetch(ctx, s, id, false)
}

// List 按作用域与可选过滤条件列账号，游标分页。
//
// 作用域与过滤条件是**与**关系：admin 指定未绑定院校时结果为空。
// search 只匹配手机号（P1 未单独存储机型）；掩码串里的 * 先去掉。
func (r *AccountRepo) List(ctx context.Context, s corescope.Scope, schoolID string, status coreaccount.Status, search, cursor string, limit int) ([]coreaccount.Record, string, error) {
	if limit < 1 || limit > 200 {
		return nil, "", validationError("limit must be between 1 and 200")
	}
	scopeSQL, args, err := scopePredicate(s, 1, "")
	if err != nil {
		return nil, "", err
	}
	where := ` WHERE deleted_at IS NULL` + scopeSQL
	if schoolID != "" {
		args = append(args, schoolID)
		where += fmt.Sprintf(" AND school_id = $%d::uuid", len(args))
	}
	if status != "" {
		if !status.Valid() {
			return nil, "", validationError("unknown account status")
		}
		args = append(args, string(status))
		where += fmt.Sprintf(" AND status = $%d::account_status", len(args))
	}
	if term := strings.TrimSpace(strings.ReplaceAll(search, "*", "")); term != "" {
		args = append(args, term)
		where += fmt.Sprintf(" AND strpos(phone, $%d) > 0", len(args))
	}
	query, args, err := appendKeyset(`SELECT `+accountColumns+` FROM accounts`+where,
		args, cursor, "created_at", "id")
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

	items := make([]coreaccount.Record, 0, limit)
	for rows.Next() {
		record, err := scanAccount(rows)
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
	next := nextCursor(items, more, func(rec coreaccount.Record) (time.Time, string) {
		return rec.CreatedAt, rec.ID
	})
	return items, next, nil
}

// UpdatePhone 在范围内改登录手机号。
// 与另一活跃行同校同号时撞唯一索引，映射为 conflict。
func (r *AccountRepo) UpdatePhone(ctx context.Context, s corescope.Scope, id, phone string, at time.Time) (coreaccount.Record, error) {
	if strings.TrimSpace(phone) == "" {
		return coreaccount.Record{}, validationError("phone is required")
	}
	scopeSQL, scopeArgs, err := scopePredicate(s, 2, "")
	if err != nil {
		return coreaccount.Record{}, err
	}
	// 作用域参数必须紧跟 $1，脚本编号再顺延。
	args := append([]any{id}, scopeArgs...)
	phoneIdx := len(args) + 1
	args = append(args, phone)
	atIdx := len(args) + 1
	args = append(args, at)
	row := r.db.q(ctx).QueryRow(ctx, fmt.Sprintf(`
		UPDATE accounts
		   SET phone = $%d, updated_at = $%d
		 WHERE id = $1::uuid AND deleted_at IS NULL%s
		RETURNING %s`, phoneIdx, atIdx, scopeSQL, accountColumns), args...)
	record, err := scanAccount(row)
	return recordOrNotFound(record, err)
}

// Review 只推进 pending 行：通过写 active，拒绝写 rejected 并记理由。
// 零行由 classifyMiss 区分：越权或已删报 not_found，状态已变报冲突。
func (r *AccountRepo) Review(ctx context.Context, s corescope.Scope, id string, approve bool, reason, by string, at time.Time) (coreaccount.Record, error) {
	if strings.TrimSpace(by) == "" {
		return coreaccount.Record{}, validationError("reviewer is required")
	}
	if !approve && strings.TrimSpace(reason) == "" {
		return coreaccount.Record{}, validationError("reject reason is required")
	}
	scopeSQL, scopeArgs, err := scopePredicate(s, 2, "")
	if err != nil {
		return coreaccount.Record{}, err
	}
	args := append([]any{id}, scopeArgs...)
	approveIdx := len(args) + 1
	args = append(args, approve)
	reasonIdx := len(args) + 1
	args = append(args, reason)
	atIdx := len(args) + 1
	args = append(args, at)
	byIdx := len(args) + 1
	args = append(args, by)
	row := r.db.q(ctx).QueryRow(ctx, fmt.Sprintf(`
		UPDATE accounts
		   SET status = CASE WHEN $%d THEN 'active'::account_status ELSE 'rejected'::account_status END,
		       reject_reason = CASE WHEN $%d THEN reject_reason ELSE $%d END,
		       reviewed_at = $%d,
		       reviewed_by = $%d::uuid,
		       updated_at = $%d
		 WHERE id = $1::uuid
		   AND status = 'pending'::account_status
		   AND deleted_at IS NULL%s
		RETURNING %s`, approveIdx, approveIdx, reasonIdx, atIdx, byIdx, atIdx, scopeSQL, accountColumns), args...)
	record, err := scanAccount(row)
	if errors.Is(err, pgx.ErrNoRows) {
		return r.classifyMiss(ctx, s, id)
	}
	return record, mapError(err)
}

// SetStatus 仅当当前状态等于 from 时改状态。
// 零行由 classifyMiss 区分：越权报 not_found，状态已变报冲突。
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

// SoftDelete 把一个账号标记为已删除。
// 行保留 30 天，误删可恢复；凭据行一并保留。
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

// Restore 在有空余容量时清除软删标记。
//
// **必须在事务内运行**。软删行不计入上限，
// 故恢复必须在锁下重新计数；
// 提前计数会让两个并发恢复把总数推过上限。
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

// Credential 在范围内返回某账号的凭据行。
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

// InsertCredential 存储账号的首条凭据。
// 复合外键拒绝与账号院校不一致的院校。
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
	// 账号缺失，或院校与账号院校不一致，
	// 都会破坏复合外键；父行找不到（§2.4）。
	return mapInsertForeignKey(err)
}

// ReplaceCredential 用乐观锁覆盖凭据。
//
// 版本号由语句自增，**绝不由调用方提供**：
// 若采信入参版本，两个并发写会同时成功，
// 后者静默覆盖前者（§7.3）。
// expectedVersion 过期时什么都不改，报冲突。
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
		       version = version + 1,
		       rekeyed_at = COALESCE($8, rekeyed_at),
		       last_verified_at = COALESCE($9, last_verified_at),
		       updated_at = now()
		 WHERE account_id = $1::uuid AND school_id = $2::uuid AND version = $10`,
		in.AccountID, in.SchoolID, in.Alg, in.KID, in.AADVersion,
		in.Nonce, in.Ciphertext, in.RekeyedAt, in.LastVerifiedAt,
		expectedVersion)
	if err != nil {
		return mapError(err)
	}
	if tag.RowsAffected() == 0 {
		return conflictError("credential version changed")
	}
	return nil
}

// fetch 在范围内读一个账号，可选是否含软删行。
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

// classifyMiss 解释一次零行比较并交换：
// 不可见的行报 not_found，可见但状态不同的行报冲突。
func (r *AccountRepo) classifyMiss(ctx context.Context, s corescope.Scope, id string) (coreaccount.Record, error) {
	if _, err := r.fetch(ctx, s, id, false); err != nil {
		return coreaccount.Record{}, err
	}
	return coreaccount.Record{}, conflictError("account status changed")
}

// classifyRestoreMiss 解释一次零行恢复。
// 行未删除报冲突；已删除但无空位报容量超限。
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
