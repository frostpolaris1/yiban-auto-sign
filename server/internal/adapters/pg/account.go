package pg

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

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

// accountDisplayNameSQL 是有效备注名的唯一 SQL 口径。
// name 为空时派生「账号+手机尾 4 位」：列表展示与搜索都读这一个值，
// 不允许一处看得到、另一处搜不到。
const accountDisplayNameSQL = `coalesce(nullif(name, ''), '账号' || right(phone, 4))`

// accountColumns 是账号读取的列清单。
//
// 属主邮箱用相关子查询取：账号行的 owner_user_id 始终指向未删用户，
// 故邮箱必定存在。明文只进 Record，入口层掩码后才出服务端。
const accountColumns = `id::text, school_id::text, owner_user_id::text, phone, name, ` +
	accountDisplayNameSQL + `,
	status::text, reject_reason, reviewed_at, coalesce(reviewed_by::text, ''),
	created_at, updated_at, deleted_at, coalesce(deleted_by::text, ''),
	coalesce((SELECT u.email FROM users u WHERE u.id = accounts.owner_user_id), '')`

const credentialColumns = `c.account_id::text, c.school_id::text, c.alg, c.kid,
	c.aad_version, c.nonce, c.ciphertext, c.version, c.created_at, c.updated_at,
	c.rekeyed_at, c.last_verified_at`

// Create 在活跃账号数低于 limit 时插入账号。
//
// **必须在事务内运行**：advisory lock、条件插入与凭据/审计行
// 同属一个工作单元。达到上限时条件 INSERT 返回零行，
// 映射为 capacity_exceeded。
func (r *AccountRepo) Create(ctx context.Context, in coreaccount.New, limit, ownerLimit int) (coreaccount.Record, error) {
	if !r.db.hasTx(ctx) {
		return coreaccount.Record{}, ErrNoTransaction
	}
	if limit <= 0 {
		return coreaccount.Record{}, validationError("limit must be positive")
	}
	if ownerLimit <= 0 {
		return coreaccount.Record{}, validationError("owner limit must be positive")
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
		WITH live AS (SELECT count(*) AS n FROM accounts WHERE deleted_at IS NULL),
		     mine AS (SELECT count(*) AS n FROM accounts
		               WHERE deleted_at IS NULL AND owner_user_id = $3::uuid)
		INSERT INTO accounts (id, school_id, owner_user_id, phone, name)
		SELECT $1::uuid, $2::uuid, $3::uuid, $4, $5
		  FROM live, mine
		 WHERE live.n < $6 AND mine.n < $7
		RETURNING `+accountColumns,
		id, in.SchoolID, in.OwnerUserID, in.Phone, in.Name, limit, ownerLimit)
	record, err := scanAccount(row)
	if errors.Is(err, pgx.ErrNoRows) {
		// 两个上限共用 capacity_exceeded；文案区分全局与每属主。
		var mine, total int
		if qerr := q.QueryRow(ctx,
			`SELECT count(*) FILTER (WHERE owner_user_id = $1::uuid), count(*)
			   FROM accounts WHERE deleted_at IS NULL`, in.OwnerUserID).Scan(&mine, &total); qerr == nil {
			if mine >= ownerLimit {
				return coreaccount.Record{}, ownerCapacityError()
			}
		}
		return coreaccount.Record{}, capacityError()
	}
	// 院校或属主缺失属父行缺失，不是冲突。
	return record, mapInsertForeignKey(err)
}

// Get 在范围内返回一个未删除账号。
func (r *AccountRepo) Get(ctx context.Context, s corescope.Scope, id string) (coreaccount.Record, error) {
	return r.fetch(ctx, s, id, false)
}

// GetIncludingDeleted 在范围内返回一个账号，软删行也返回。
// 本人自助面用它读自己刚软删的行：Get 对软删行报 404，
// 无法区分「不是我的」与「已删」。
func (r *AccountRepo) GetIncludingDeleted(ctx context.Context, s corescope.Scope, id string) (coreaccount.Record, error) {
	return r.fetch(ctx, s, id, true)
}

// GetForUpdate 在范围内返回一个未删除账号，并持有行锁到事务结束。
//
// **必须在事务内运行**：无事务报 ErrNoTransaction。
// 恢复判定用它在锁下读状态：事务外的快照会漏掉已提交的 credential_error。
// 锁把引擎的状态写与本次凭据更新串行化，两者不再交叉判定。
func (r *AccountRepo) GetForUpdate(ctx context.Context, s corescope.Scope, id string) (coreaccount.Record, error) {
	if !r.db.hasTx(ctx) {
		return coreaccount.Record{}, ErrNoTransaction
	}
	scopeSQL, scopeArgs, err := scopePredicate(s, 2, "")
	if err != nil {
		return coreaccount.Record{}, err
	}
	args := append([]any{id}, scopeArgs...)
	row := r.db.q(ctx).QueryRow(ctx,
		`SELECT `+accountColumns+` FROM accounts WHERE id = $1::uuid AND deleted_at IS NULL`+scopeSQL+` FOR UPDATE`, args...)
	record, err := scanAccount(row)
	return recordOrNotFound(record, err)
}

// Touch 在范围内推进 accounts.updated_at 并回读整行。
//
// **必须在事务内运行**：仅改凭据的更新只写 credentials，
// 不推进账号行时间戳就会让并发凭据编辑绕过乐观锁前置条件。
// at 仅是与同族更新同形的占位实参：accounts 的 set_updated_at()
// 触发器会用库内 now() 覆盖它，实际落库取数据库时钟。
// 因此 Touch 与调用方时钟无关，跨事务的时间戳仍然前进。
func (r *AccountRepo) Touch(ctx context.Context, s corescope.Scope, id string, at time.Time) (coreaccount.Record, error) {
	if !r.db.hasTx(ctx) {
		return coreaccount.Record{}, ErrNoTransaction
	}
	scopeSQL, scopeArgs, err := scopePredicate(s, 2, "")
	if err != nil {
		return coreaccount.Record{}, err
	}
	args := append([]any{id}, scopeArgs...)
	atIdx := len(args) + 1
	args = append(args, at)
	row := r.db.q(ctx).QueryRow(ctx, fmt.Sprintf(`
		UPDATE accounts
		   SET updated_at = $%d
		 WHERE id = $1::uuid AND deleted_at IS NULL%s
		RETURNING %s`, atIdx, scopeSQL, accountColumns), args...)
	record, err := scanAccount(row)
	return recordOrNotFound(record, err)
}

// List 按作用域与可选过滤条件列账号，游标分页。
//
// 作用域与过滤条件是**与**关系：admin 指定未绑定院校时结果为空。
// includeDeleted 置真时软删行也入选；缺省假，已删行不可发现。
// search 同时匹配备注名、属主邮箱与手机号；手机号仍只按
// 1..3 位前缀、4 位后缀、11 位全号精确命中，故中四位可被全号
// 枚举恢复（10^4 次），依赖登录限速。
func (r *AccountRepo) List(ctx context.Context, s corescope.Scope, schoolID string, status coreaccount.Status, search, cursor string, limit int, includeDeleted bool) ([]coreaccount.Record, string, error) {
	if limit < 1 || limit > 200 {
		return nil, "", validationError("limit must be between 1 and 200")
	}
	scopeSQL, args, err := scopePredicate(s, 1, "")
	if err != nil {
		return nil, "", err
	}
	where := ` WHERE deleted_at IS NULL`
	if includeDeleted {
		where = ` WHERE true`
	}
	where += scopeSQL
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
	where += accountSearchWhere(search, &args)
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

// StatusCounts 按作用域统计未软删账号的 pending 与 rejected 行数。
// 口径与 List 缺省一致：deleted_at IS NULL 才计入；软删行不进任何计数。
// 一次扫描出两个计数；其余状态不进本结果。
func (r *AccountRepo) StatusCounts(ctx context.Context, s corescope.Scope) (coreaccount.StatusCounts, error) {
	scopeSQL, args, err := scopePredicate(s, 1, "")
	if err != nil {
		return coreaccount.StatusCounts{}, err
	}
	query := `SELECT count(*) FILTER (WHERE status = 'pending'),
		count(*) FILTER (WHERE status = 'rejected')
		FROM accounts WHERE deleted_at IS NULL` + scopeSQL
	var counts coreaccount.StatusCounts
	if err := r.db.q(ctx).QueryRow(ctx, query, args...).Scan(&counts.Pending, &counts.Rejected); err != nil {
		return coreaccount.StatusCounts{}, mapError(err)
	}
	return counts, nil
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

// UpdateName 在范围内改备注名。空串表示清空。
// 长度由调用方与 ck_accounts_name_length 双重把关。
func (r *AccountRepo) UpdateName(ctx context.Context, s corescope.Scope, id, name string, at time.Time) (coreaccount.Record, error) {
	scopeSQL, scopeArgs, err := scopePredicate(s, 2, "")
	if err != nil {
		return coreaccount.Record{}, err
	}
	args := append([]any{id}, scopeArgs...)
	nameIdx := len(args) + 1
	args = append(args, name)
	atIdx := len(args) + 1
	args = append(args, at)
	row := r.db.q(ctx).QueryRow(ctx, fmt.Sprintf(`
		UPDATE accounts
		   SET name = $%d, updated_at = $%d
		 WHERE id = $1::uuid AND deleted_at IS NULL%s
		RETURNING %s`, nameIdx, atIdx, scopeSQL, accountColumns), args...)
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
// to 为 pending 时同时清空驳回理由与审核戳：pending 行不属于任何已审轮次。
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
		       reject_reason = CASE
		           WHEN $%d::account_status = 'rejected' THEN $%d
		           WHEN $%d::account_status = 'pending' THEN ''
		           ELSE reject_reason
		       END,
		       reviewed_at = CASE
		           WHEN $%d::account_status = 'pending' THEN NULL
		           ELSE reviewed_at
		       END,
		       reviewed_by = CASE
		           WHEN $%d::account_status = 'pending' THEN NULL
		           ELSE reviewed_by
		       END,
		       updated_at = $%d
		 WHERE id = $1::uuid
		   AND status = $%d::account_status
		   AND deleted_at IS NULL%s
		RETURNING %s`,
		next, next, next+1, next, next, next, next+2, next+3, scopeSQL, accountColumns)
	args = append(args, string(to), reason, at, string(from))
	row := r.db.q(ctx).QueryRow(ctx, query, args...)
	record, err := scanAccount(row)
	if errors.Is(err, pgx.ErrNoRows) {
		return r.classifyMiss(ctx, s, id)
	}
	return record, mapError(err)
}

// SoftDelete 把一个账号标记为已删除。
// 行保留到保留期任务物理清除；本人自助恢复窗口见
// core/account.SelfRestoreGrace（7 天）。凭据行一并保留。
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

// GetDeleted 在范围内锁定并读取一个账号行，软删与活行都返回。
// **必须在事务内运行**：FOR UPDATE 持锁到事务结束，与恢复互斥——
// 恢复方等锁，不会出现「校验为已删后被并发恢复仍被物理删除」。
// 零行分类：不存在或越权行报 not_found；活行（未软删）报 conflict。
// 物理清除用它拿审计所需的 school_id 等列，不能用排除软删行的 Get。
func (r *AccountRepo) GetDeleted(ctx context.Context, s corescope.Scope, id string) (coreaccount.Record, error) {
	if !r.db.hasTx(ctx) {
		return coreaccount.Record{}, ErrNoTransaction
	}
	scopeSQL, scopeArgs, err := scopePredicate(s, 2, "")
	if err != nil {
		return coreaccount.Record{}, err
	}
	args := append([]any{id}, scopeArgs...)
	row := r.db.q(ctx).QueryRow(ctx,
		`SELECT `+accountColumns+` FROM accounts WHERE id = $1::uuid`+scopeSQL+` FOR UPDATE`, args...)
	record, err := scanAccount(row)
	if errors.Is(err, pgx.ErrNoRows) {
		return coreaccount.Record{}, coreerrs.ErrNotFound
	}
	if err != nil {
		return coreaccount.Record{}, mapError(err)
	}
	if record.DeletedAt == nil {
		return coreaccount.Record{}, conflictError("account is not deleted")
	}
	return record, nil
}

// Purge 物理删除一个账号及其从属行。
// **必须在事务内运行**：调用方先用 GetDeleted 锁行并校验软删状态，
// 本方法按 id 集合删除，不带作用域重判。顺序与保留期任务一致：
// 先分块删 sign_tasks（RESTRICT），再删账号行；
// credentials、account_egress、account_location 随外键级联。
// 行已锁定仍删不到是数据事故，报 internal 而不是静默成功。
func (r *AccountRepo) Purge(ctx context.Context, id string) error {
	if !r.db.hasTx(ctx) {
		return ErrNoTransaction
	}
	deleted, err := purgeAccountsByIDs(ctx, r.db, []string{id}, nil)
	if err != nil {
		return err
	}
	if deleted != 1 {
		return coreerrs.New(coreerrs.CodeInternal, "purge deleted an unexpected number of accounts")
	}
	return nil
}

// Restore 在有空余容量时清除软删标记。
//
// **必须在事务内运行**。软删行不计入上限，
// 故恢复必须在锁下重新计数；
// 提前计数会让两个并发恢复把总数推过上限。
func (r *AccountRepo) Restore(ctx context.Context, s corescope.Scope, id string, limit, ownerLimit int) (coreaccount.Record, error) {
	if !r.db.hasTx(ctx) {
		return coreaccount.Record{}, ErrNoTransaction
	}
	if limit <= 0 {
		return coreaccount.Record{}, validationError("limit must be positive")
	}
	if ownerLimit <= 0 {
		return coreaccount.Record{}, validationError("owner limit must be positive")
	}
	if _, err := r.db.q(ctx).Exec(ctx, "SELECT pg_advisory_xact_lock($1)", AccountCapacityLock); err != nil {
		return coreaccount.Record{}, mapError(err)
	}
	scopeSQL, scopeArgs, err := scopePredicate(s, 4, "")
	if err != nil {
		return coreaccount.Record{}, err
	}
	args := append([]any{id, limit, ownerLimit}, scopeArgs...)
	row := r.db.q(ctx).QueryRow(ctx, `
		UPDATE accounts
		   SET deleted_at = NULL, deleted_by = NULL, updated_at = now()
		 WHERE id = $1::uuid
		   AND deleted_at IS NOT NULL
		   AND (SELECT count(*) FROM accounts WHERE deleted_at IS NULL) < $2
		   AND (SELECT count(*) FROM accounts
		         WHERE deleted_at IS NULL
		           AND owner_user_id = (SELECT owner_user_id FROM accounts WHERE id = $1::uuid)) < $3`+scopeSQL+`
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

// RestoreSelf 恢复本人自删的账号。
//
// **必须在事务内运行**。条件 UPDATE 一次判完全部自助判据：
// 只恢复 deleted_by=by 的行、deleted_at 不早于 notBefore（宽限窗口）、
// 且该属主名下没有其他未软删账号；容量与管理员恢复同口径，
// 在同一个容量 advisory lock 下重数。
//
// 零行分类：不可见或不存在 → not_found；未删 → conflict；
// 删除者不是本人 → forbidden；超窗 → conflict；名下已有生效账号 →
// validation；其余按容量不足报 capacity_exceeded。
func (r *AccountRepo) RestoreSelf(ctx context.Context, s corescope.Scope, id, by string, notBefore time.Time, limit, ownerLimit int) (coreaccount.Record, error) {
	if !r.db.hasTx(ctx) {
		return coreaccount.Record{}, ErrNoTransaction
	}
	if strings.TrimSpace(by) == "" {
		return coreaccount.Record{}, validationError("deleted_by is required")
	}
	if notBefore.IsZero() {
		return coreaccount.Record{}, validationError("grace cutoff is required")
	}
	if limit <= 0 || ownerLimit <= 0 {
		return coreaccount.Record{}, validationError("limit must be positive")
	}
	if _, err := r.db.q(ctx).Exec(ctx, "SELECT pg_advisory_xact_lock($1)", AccountCapacityLock); err != nil {
		return coreaccount.Record{}, mapError(err)
	}
	scopeSQL, scopeArgs, err := scopePredicate(s, 6, "")
	if err != nil {
		return coreaccount.Record{}, err
	}
	args := append([]any{id, limit, ownerLimit, notBefore, by}, scopeArgs...)
	row := r.db.q(ctx).QueryRow(ctx, `
		UPDATE accounts
		   SET deleted_at = NULL, deleted_by = NULL, updated_at = now()
		 WHERE id = $1::uuid
		   AND deleted_at IS NOT NULL
		   AND deleted_at >= $4
		   AND deleted_by = $5::uuid
		   AND NOT EXISTS (SELECT 1 FROM accounts live
		                    WHERE live.owner_user_id = accounts.owner_user_id
		                      AND live.deleted_at IS NULL)
		   AND (SELECT count(*) FROM accounts WHERE deleted_at IS NULL) < $2
		   AND (SELECT count(*) FROM accounts
		         WHERE deleted_at IS NULL
		           AND owner_user_id = (SELECT owner_user_id FROM accounts WHERE id = $1::uuid)) < $3`+scopeSQL+`
		RETURNING `+accountColumns, args...)
	record, err := scanAccount(row)
	if err == nil {
		return record, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return coreaccount.Record{}, mapError(err)
	}
	return r.classifySelfRestoreMiss(ctx, s, id, by, notBefore)
}

// classifySelfRestoreMiss 解释一次零行自助恢复，按判据顺序给出稳定错误。
// 读取在 UPDATE 之后：同事务的后续语句看到的是最新已提交快照。
func (r *AccountRepo) classifySelfRestoreMiss(ctx context.Context, s corescope.Scope, id, by string, notBefore time.Time) (coreaccount.Record, error) {
	record, err := r.fetch(ctx, s, id, true)
	if err != nil {
		return coreaccount.Record{}, err
	}
	switch {
	case record.DeletedAt == nil:
		return coreaccount.Record{}, conflictError("account is not deleted")
	case record.DeletedBy != by:
		return coreaccount.Record{}, coreerrs.New(coreerrs.CodeForbidden,
			"only a self-deleted account can be restored")
	case record.DeletedAt.Before(notBefore):
		return coreaccount.Record{}, conflictError("the self-restore grace period has expired")
	}
	var live int
	if err := r.db.q(ctx).QueryRow(ctx,
		`SELECT count(*) FROM accounts WHERE owner_user_id = $1::uuid AND deleted_at IS NULL`,
		record.OwnerUserID).Scan(&live); err != nil {
		return coreaccount.Record{}, mapError(err)
	}
	if live > 0 {
		return coreaccount.Record{}, validationError("an active account already exists")
	}
	return coreaccount.Record{}, capacityError()
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

// Credentials 批量读未软删账号的凭据，按账号 ID 建索引。
// 空 ids 不发查询，直接回空 map。
// 软删行与作用域外行不出现在结果里；缺失的行不报错。
func (r *AccountRepo) Credentials(ctx context.Context, s corescope.Scope, ids []string) (map[string]coreaccount.Credential, error) {
	out := make(map[string]coreaccount.Credential, len(ids))
	if len(ids) == 0 {
		return out, nil
	}
	scopeSQL, scopeArgs, err := scopePredicate(s, 2, "a.")
	if err != nil {
		return nil, err
	}
	args := append([]any{ids}, scopeArgs...)
	rows, err := r.db.q(ctx).Query(ctx, `
		SELECT `+credentialColumns+`
		  FROM credentials c
		  JOIN accounts a ON a.id = c.account_id
		 WHERE c.account_id = ANY($1::text[]::uuid[]) AND a.deleted_at IS NULL`+scopeSQL, args...)
	if err != nil {
		return nil, mapError(err)
	}
	defer rows.Close()
	for rows.Next() {
		record, err := scanCredential(rows)
		if err != nil {
			return nil, mapError(err)
		}
		out[record.AccountID] = record
	}
	if err := rows.Err(); err != nil {
		return nil, mapError(err)
	}
	return out, nil
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
	// 都会破坏复合外键；父行找不到。
	return mapInsertForeignKey(err)
}

// ReplaceCredential 用乐观锁覆盖凭据。
//
// 版本号由语句自增，**绝不由调用方提供**：
// 若采信入参版本，两个并发写会同时成功，
// 后者静默覆盖前者。
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

// accountSearchWhere 把列表搜索串翻译成 WHERE 片段。
//
// 完整手机号与完整邮箱走**精确直达**：管理员输入已知的完整标识时，
// 结果只剩该账号（或该属主名下的全部账号），不与名称子串混在一起。
// 手机号先归一 +86/86 前缀与分隔符；邮箱要求恰好一个 @、两侧非空且无空白。
// 其余输入走发现面：
//   - 有效备注名（name 或派生的「账号+手机尾 4 位」）按字面子串匹配，
//     故搜「账号」能一次找到所有未命名账号；position 不解释 % 与 _。
//   - 可见邮箱掩码：串里含 "***" 时星号段按任意字符处理；掩码公式仍只在
//     core/mask，这里只比对可见的前后缀。
//   - 手机号前 1..3 位与后 4 位仍按前缀/后缀匹配；5..10 位不参与号码匹配，
//     但仍可命中备注名或掩码邮箱。
//
// 不按原始邮箱做部分子串：完整邮箱已可精确直达，部分输入用可见掩码即可，
// 避免把一个可枚举的明文检索面暴露出去。
func accountSearchWhere(search string, args *[]any) string {
	term := strings.TrimSpace(search)
	if term == "" {
		return ""
	}
	if digits, ok := completePhoneQuery(term); ok {
		// 同值的数字备注名也算精确命中：手机号形状的备注不会被完整号码挡掉。
		*args = append(*args, digits)
		return fmt.Sprintf(" AND (phone = $%d OR lower(%s) = $%d)",
			len(*args), accountDisplayNameSQL, len(*args))
	}
	if isCompleteEmailQuery(term) {
		lower := strings.ToLower(term)
		*args = append(*args, lower)
		return fmt.Sprintf(` AND (lower(coalesce(
			(SELECT u.email FROM users u WHERE u.id = accounts.owner_user_id), '')) = $%d
			OR lower(%s) = $%d)`, len(*args), accountDisplayNameSQL, len(*args))
	}
	lower := strings.ToLower(term)
	*args = append(*args, lower)
	termIdx := len(*args)
	clauses := []string{
		fmt.Sprintf("position($%d in lower(%s)) > 0", termIdx, accountDisplayNameSQL),
	}
	if prefix, suffix, ok := emailMaskParts(lower); ok {
		*args = append(*args, prefix)
		prefixIdx := len(*args)
		*args = append(*args, suffix)
		suffixIdx := len(*args)
		// 邮箱库内恒为小写（ck_users_email_lower），入参已转小写。
		clauses = append(clauses, fmt.Sprintf(`EXISTS (
			SELECT 1 FROM users u
			 WHERE u.id = accounts.owner_user_id
			   AND left(u.email, char_length($%d)) = $%d
			   AND right(u.email, char_length($%d)) = $%d)`,
			prefixIdx, prefixIdx, suffixIdx, suffixIdx))
	}
	if digits := phoneQueryDigits(term); digits != "" {
		// 参数只在真正生成子句时追加：5..10 位不参与号码匹配，
		// 多传一个未引用的参数会让扩展协议报参数个数不符。
		switch {
		case len(digits) <= 3:
			// 前缀：只暴露前 3 位，掩码本就展示这 3 位。
			*args = append(*args, digits)
			clauses = append(clauses, fmt.Sprintf("phone LIKE $%d || '%%'", len(*args)))
		case len(digits) == 4:
			// 后缀：只暴露后 4 位，掩码本就展示这 4 位。
			*args = append(*args, digits)
			clauses = append(clauses, fmt.Sprintf("phone LIKE '%%' || $%d", len(*args)))
		}
	}
	return " AND (" + strings.Join(clauses, " OR ") + ")"
}

// isPhoneQueryShape 判断输入是否只由数字与常见电话分隔符组成。
// 含 @、字母或其他字符的串不是手机号：13800138000@example.com 是合法邮箱，
// 不能被剥成 11 位数字后当成手机号；备注名里的字母也不该触发号码匹配。
func isPhoneQueryShape(term string) bool {
	hasDigit := false
	for _, r := range term {
		switch {
		case r >= '0' && r <= '9':
			hasDigit = true
		case r == '+' || r == '-' || r == '.' || r == '(' || r == ')' || unicode.IsSpace(r):
		default:
			return false
		}
	}
	return hasDigit
}

// completePhoneQuery 归一完整手机号：仅接受电话形状，13 位且以 86 开头时去掉国家码。
// 返回 11 位号码与是否完整。
func completePhoneQuery(term string) (string, bool) {
	if !isPhoneQueryShape(term) {
		return "", false
	}
	digits := phoneDigits(term)
	if len(digits) == 13 && strings.HasPrefix(digits, "86") {
		digits = digits[2:]
	}
	if len(digits) == 11 {
		return digits, true
	}
	return "", false
}

// phoneQueryDigits 给部分输入用：只保留 11 位或 +86/86 前缀的 13 位。
// 非电话形状返回空串：字母串只走备注名与掩码邮箱。
func phoneQueryDigits(term string) string {
	if !isPhoneQueryShape(term) {
		return ""
	}
	digits := phoneDigits(term)
	if len(digits) == 13 && strings.HasPrefix(digits, "86") {
		return digits[2:]
	}
	return digits
}

// isCompleteEmailQuery 判断输入是否是一个完整邮箱形状。
// 与 users.email 的 CHECK 同形：恰好一个 @、两侧非空、无空白，且不含 *。
// 含 *** 的串是可见掩码，交给掩码分支，不能当成完整邮箱去等值比较。
func isCompleteEmailQuery(term string) bool {
	if strings.Count(term, "@") != 1 || strings.Contains(term, "*") {
		return false
	}
	at := strings.IndexByte(term, '@')
	if at <= 0 || at == len(term)-1 {
		return false
	}
	return !strings.ContainsAny(term, " \t\r\n")
}

// emailMaskParts 从可见掩码串取前缀与后缀。
//
// 可见掩码只保留本地部首字符、隐藏其余部分，故前缀必须是**恰好一个字符**：
// 允许更长的前缀等于把隐藏的本地部逐字符喂给检索面。
// 后缀必须以 @ 开头（可见域名）；"***" 单独出现或多段星号都不算掩码。
func emailMaskParts(term string) (string, string, bool) {
	parts := strings.Split(term, "***")
	if len(parts) != 2 {
		return "", "", false
	}
	prefix, suffix := parts[0], parts[1]
	if utf8.RuneCountInString(prefix) != 1 || !strings.HasPrefix(suffix, "@") {
		return "", "", false
	}
	return prefix, suffix, true
}

// phoneDigits 只保留 search 里的 0..9。
// 掩码串的 * 与分隔符在此消失；没有数字的串返回空串。
func phoneDigits(search string) string {
	var b strings.Builder
	for _, r := range search {
		if r >= '0' && r <= '9' {
			b.WriteRune(r)
		}
	}
	return b.String()
}

func scanAccount(row pgx.Row) (coreaccount.Record, error) {
	var (
		record     coreaccount.Record
		status     string
		reviewedAt pgtype.Timestamptz
		deletedAt  pgtype.Timestamptz
	)
	if err := row.Scan(&record.ID, &record.SchoolID, &record.OwnerUserID, &record.Phone, &record.Name,
		&record.DisplayName, &status, &record.RejectReason, &reviewedAt, &record.ReviewedBy,
		&record.CreatedAt, &record.UpdatedAt, &deletedAt, &record.DeletedBy, &record.OwnerEmail); err != nil {
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
