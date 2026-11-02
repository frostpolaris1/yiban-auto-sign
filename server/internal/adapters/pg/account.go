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

// accountColumns 是账号读取的列清单。
//
// 属主邮箱用相关子查询取：账号行的 owner_user_id 始终指向未删用户，
// 故邮箱必定存在。明文只进 Record，入口层掩码后才出服务端。
const accountColumns = `id::text, school_id::text, owner_user_id::text, phone, name,
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
// 命中面是三列之或：备注名、属主邮箱、手机号。
// 名称与邮箱按字面子串、大小写不敏感匹配：position 不做通配解读，
// 用户输入 % 或 _ 不会被当成 LIKE 元字符。
// 邮箱还接受**可见掩码**：串里含 "***" 时星号段按任意字符处理，
// 故 a***@example.com 能命中同一掩码的全部账号；掩码公式仍只在 core/mask，
// 这里只比对可见的前后缀，不复制掩码实现。
// 手机号沿用既有长度口径：1..3 位前缀、4 位后缀、11 位全号；
// 5..10 位不参与号码匹配，但仍可命中名称或邮箱。
// 空串不过滤。
func accountSearchWhere(search string, args *[]any) string {
	term := strings.TrimSpace(search)
	if term == "" {
		return ""
	}
	lower := strings.ToLower(term)
	*args = append(*args, lower)
	termIdx := len(*args)
	clauses := []string{
		fmt.Sprintf("position($%d in lower(name)) > 0", termIdx),
		fmt.Sprintf(`position($%d in lower(coalesce(
			(SELECT u.email FROM users u WHERE u.id = accounts.owner_user_id), ''))) > 0`, termIdx),
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
	if digits := phoneDigits(term); digits != "" {
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
		case len(digits) == 11:
			*args = append(*args, digits)
			clauses = append(clauses, fmt.Sprintf("phone = $%d", len(*args)))
		}
	}
	return " AND (" + strings.Join(clauses, " OR ") + ")"
}

// emailMaskParts 从可见掩码串取前缀与后缀。
// 只处理恰好一个 "***" 的输入：多个星号段不是可识别的掩码形状，不启用该分支。
func emailMaskParts(term string) (string, string, bool) {
	parts := strings.Split(term, "***")
	if len(parts) != 2 {
		return "", "", false
	}
	if parts[0] == "" && parts[1] == "" {
		return "", "", false
	}
	return parts[0], parts[1], true
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
		&status, &record.RejectReason, &reviewedAt, &record.ReviewedBy,
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
