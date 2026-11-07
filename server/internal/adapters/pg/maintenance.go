package pg

// 过期数据清理的仓储。清六张表：会话、重置令牌、旧任务行、旧站内信、
// 软删满期的账号与用户。
//
// 审计链（audit_logs）不在这里清：它是 append-only，清理只能走迁移级
// 操作（重写 guard），否则保留期与不可篡改互相打架。见 core/audit 口径。

import (
	"context"
	"time"

	"github.com/jackc/pgx/v5"
)

// MaintenanceRepo 清理过期数据。每批删除有上限：长事务会挡住认领与登录。
type MaintenanceRepo struct{ db *DB }

// NewMaintenanceRepo 构造清理仓储。
func NewMaintenanceRepo(db *DB) *MaintenanceRepo { return &MaintenanceRepo{db: db} }

// DeleteExpiredSessions 删除 expires_at < before 的会话，每批至多 limit 行。
func (r *MaintenanceRepo) DeleteExpiredSessions(ctx context.Context, before time.Time, limit int) (int64, error) {
	return r.deleteBatch(ctx, `
		DELETE FROM sessions WHERE id IN (
		  SELECT id FROM sessions WHERE expires_at < $1
		   ORDER BY expires_at LIMIT $2)`, before, limit)
}

// DeleteExpiredPasswordResets 删除 expires_at < before 的重置令牌，每批至多 limit 行。
func (r *MaintenanceRepo) DeleteExpiredPasswordResets(ctx context.Context, before time.Time, limit int) (int64, error) {
	return r.deleteBatch(ctx, `
		DELETE FROM password_resets WHERE id IN (
		  SELECT id FROM password_resets WHERE expires_at < $1
		   ORDER BY expires_at LIMIT $2)`, before, limit)
}

// DeleteTasksBefore 删除 day < before（YYYY-MM-DD）的签到任务行，每批至多 limit 行。
func (r *MaintenanceRepo) DeleteTasksBefore(ctx context.Context, before string, limit int) (int64, error) {
	return r.deleteBatch(ctx, `
		DELETE FROM sign_tasks WHERE id IN (
		  SELECT id FROM sign_tasks WHERE day < $1::date
		   ORDER BY day LIMIT $2)`, before, limit)
}

// DeleteNotificationsBefore 删除 created_at < before 的站内信，每批至多 limit 行。
// 投递账本随外键级联删除；本方法不碰审计链。
func (r *MaintenanceRepo) DeleteNotificationsBefore(ctx context.Context, before time.Time, limit int) (int64, error) {
	return deleteNotificationsBefore(ctx, r.db, before, limit)
}

// signTaskChunkSize 是任务行分块删除的单语句行数上限：
// 一次删千账号的全部任务行可能撞 statement_timeout。
const signTaskChunkSize = 1000

// DeleteAccountsDeletedBefore 物理删除 deleted_at < before 的软删账号，每批至多 limit 行。
// 先分块删该批账号的 sign_tasks（RESTRICT 外键），再删账号；
// credentials、account_egress、account_location 随外键级联。
// 选 id（FOR UPDATE）与删除同在一个事务：单批失败整批回滚，
// 恢复方在锁上等待，不会出现「选出后被恢复仍删从属行」。返回删掉的账号数。
func (r *MaintenanceRepo) DeleteAccountsDeletedBefore(ctx context.Context, before time.Time, limit int) (int64, error) {
	var deleted int64
	err := r.db.InTx(ctx, func(ctx context.Context) error {
		ids, err := selectSoftDeletedAccountIDs(ctx, r.db, before, limit)
		if err != nil || len(ids) == 0 {
			return err
		}
		deleted, err = purgeAccountsByIDs(ctx, r.db, ids, &before)
		return err
	})
	if err != nil {
		return 0, err
	}
	return deleted, nil
}

// DeleteUsersDeletedBefore 物理删除 deleted_at < before 的软删用户，每批至多 limit 行。
// 顺序钉死在外键上：先分块删名下账号的 sign_tasks、再删其账号；
// 审核对与定位引用先成对处置（RI 置 NULL 会撞 ck_accounts_review_pair，
// updated_by 无 ON DELETE 会拦下删用户），最后删用户；
// window_overrides.created_by 随 00027 的 SET NULL 置空（覆盖按日生效，
// 不随创建者清除），sessions、time_prefs、photos、notifications 等级联。
// 成对清 reviewed_at/reviewed_by 是为满足 ck_accounts_review_pair；
// 审计链 append-only，review 事实仍完整保留。
// 选 id（FOR UPDATE）与删除同在一个事务：单批失败整批回滚。
// 返回删掉的用户数。
func (r *MaintenanceRepo) DeleteUsersDeletedBefore(ctx context.Context, before time.Time, limit int) (int64, error) {
	var deleted int64
	err := r.db.InTx(ctx, func(ctx context.Context) error {
		ids, err := selectSoftDeletedUserIDs(ctx, r.db, before, limit)
		if err != nil || len(ids) == 0 {
			return err
		}
		deleted, err = purgeUsersByIDs(ctx, r.db, ids, &before)
		return err
	})
	if err != nil {
		return 0, err
	}
	return deleted, nil
}

// purgeAccountsByIDs 物理删除一批账号及其从属行，返回删掉的账号数。
// 顺序：先分块删该批账号的 sign_tasks（RESTRICT 外键），再删账号；
// credentials、account_egress、account_location 随外键级联。
// deletedBefore 非 nil 时只删 deleted_at 更早的行（保留期口径，重验软删）；
// 为 nil 时按 id 集合删除：调用方已在同一事务里锁定并校验过软删状态。
// 调用方保证处于事务 ctx 内；保留期路径还须先用 FOR UPDATE 锁住账号行。
func purgeAccountsByIDs(ctx context.Context, db *DB, accountIDs []string, deletedBefore *time.Time) (int64, error) {
	if err := deleteSignTaskChunks(ctx, db, accountIDs); err != nil {
		return 0, err
	}
	sqlText := `DELETE FROM accounts WHERE id = ANY($1::text[]::uuid[])`
	args := []any{accountIDs}
	if deletedBefore != nil {
		// 重验 deleted_at：批内被恢复的账号不动（活行绝不物理删除）。
		sqlText += ` AND deleted_at < $2`
		args = append(args, *deletedBefore)
	}
	ct, err := db.q(ctx).Exec(ctx, sqlText, args...)
	if err != nil {
		return 0, mapError(err)
	}
	return ct.RowsAffected(), nil
}

// purgeUsersByIDs 物理删除一批用户及其名下账号与从属行，返回删掉的用户数。
// 顺序：名下账号行锁（FOR UPDATE，与恢复互斥）→ 名下账号的任务行分块删
// （sign_tasks.account_id 是 RESTRICT）→ 删名下账号 → 审核对成对置空 →
// account_location.updated_by 置空 → 删用户行。
// window_overrides.created_by 随 00027 的 SET NULL 自动置空，覆盖按日生效，
// 不随创建者清除；sessions、time_prefs、photos、notifications 等级联。
// 成对清 reviewed_at/reviewed_by 是为满足 ck_accounts_review_pair；
// 审计链 append-only，review 事实仍完整保留。
// deletedBefore 非 nil 时只删 deleted_at 更早的用户行（保留期口径，重验软删）；
// 为 nil 时按 id 集合删除：调用方已在同一事务里锁定并校验过软删状态。
// 调用方保证处于事务 ctx 内。
func purgeUsersByIDs(ctx context.Context, db *DB, userIDs []string, deletedBefore *time.Time) (int64, error) {
	accountIDs, err := selectAccountsOfUsers(ctx, db, userIDs)
	if err != nil {
		return 0, err
	}
	// 名下账号的任务行先于账号行：sign_tasks.account_id 是 RESTRICT。
	if err := deleteSignTaskChunks(ctx, db, accountIDs); err != nil {
		return 0, err
	}
	if _, err := db.q(ctx).Exec(ctx, `
		DELETE FROM accounts WHERE owner_user_id = ANY($1::text[]::uuid[])`, userIDs); err != nil {
		return 0, mapError(err)
	}
	// 审核对是成对列：RI 只置 reviewed_by 会撞 ck_accounts_review_pair，
	// 故先成对置空（只动还活着的别人账号；本批账号已随上行删除）。
	if _, err := db.q(ctx).Exec(ctx, `
		UPDATE accounts SET reviewed_by = NULL, reviewed_at = NULL
		 WHERE reviewed_by = ANY($1::text[]::uuid[])`, userIDs); err != nil {
		return 0, mapError(err)
	}
	// 定位行的 updated_by 无 ON DELETE：引用本批用户的行置空，
	// 行本身保留（它属于可能还活着的别人账号）。
	if _, err := db.q(ctx).Exec(ctx, `
		UPDATE account_location SET updated_by = NULL
		 WHERE updated_by = ANY($1::text[]::uuid[])`, userIDs); err != nil {
		return 0, mapError(err)
	}
	sqlText := `DELETE FROM users WHERE id = ANY($1::text[]::uuid[])`
	args := []any{userIDs}
	if deletedBefore != nil {
		// 重验 deleted_at：批内被恢复的用户不动（活行绝不物理删除）。
		sqlText += ` AND deleted_at < $2`
		args = append(args, *deletedBefore)
	}
	ct, err := db.q(ctx).Exec(ctx, sqlText, args...)
	if err != nil {
		return 0, mapError(err)
	}
	return ct.RowsAffected(), nil
}

// deleteSignTaskChunks 分块删除账号集合的任务行：单语句 LIMIT 封顶，
// 删到不足一块为止。调用方保证处于事务 ctx 内。
func deleteSignTaskChunks(ctx context.Context, db *DB, accountIDs []string) error {
	for len(accountIDs) > 0 {
		ct, err := db.q(ctx).Exec(ctx, `
			DELETE FROM sign_tasks WHERE id IN (
			  SELECT id FROM sign_tasks WHERE account_id = ANY($1::text[]::uuid[])
			   LIMIT $2)`, accountIDs, signTaskChunkSize)
		if err != nil {
			return mapError(err)
		}
		if ct.RowsAffected() < signTaskChunkSize {
			return nil
		}
	}
	return nil
}

// selectSoftDeletedAccountIDs 按 (deleted_at, id) 选一批软删账号 id，limit 封顶。
// FOR UPDATE：与恢复互斥，恢复方等锁而不是被连带清掉从属行。
func selectSoftDeletedAccountIDs(ctx context.Context, db *DB, before time.Time, limit int) ([]string, error) {
	return scanIDs(db.q(ctx).Query(ctx, `
		SELECT id::text FROM accounts
		 WHERE deleted_at < $1
		 ORDER BY deleted_at, id
		 LIMIT $2
		 FOR UPDATE`, before, limit))
}

// selectSoftDeletedUserIDs 按 (deleted_at, id) 选一批软删用户 id，limit 封顶。
// FOR UPDATE：与恢复互斥，恢复方等锁而不是被连带清掉从属行。
func selectSoftDeletedUserIDs(ctx context.Context, db *DB, before time.Time, limit int) ([]string, error) {
	return scanIDs(db.q(ctx).Query(ctx, `
		SELECT id::text FROM users
		 WHERE deleted_at < $1
		 ORDER BY deleted_at, id
		 LIMIT $2
		 FOR UPDATE`, before, limit))
}

// selectAccountsOfUsers 查用户集合名下全部账号 id（含活账号：
// owner_user_id 是 RESTRICT，用户硬删前名下账号必须清空）。
// FOR UPDATE：封住「快照后账号被恢复」的窗口，恢复方等锁。
func selectAccountsOfUsers(ctx context.Context, db *DB, userIDs []string) ([]string, error) {
	return scanIDs(db.q(ctx).Query(ctx, `
		SELECT id::text FROM accounts WHERE owner_user_id = ANY($1::text[]::uuid[])
		 FOR UPDATE`, userIDs))
}

// scanIDs 收拢一批 id 文本。
func scanIDs(rows pgx.Rows, err error) ([]string, error) {
	if err != nil {
		return nil, mapError(err)
	}
	defer rows.Close()
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, mapError(err)
		}
		ids = append(ids, id)
	}
	return ids, mapError(rows.Err())
}

// deleteBatch 执行一条按 id 子查询限定批量的 DELETE。
func (r *MaintenanceRepo) deleteBatch(ctx context.Context, sql string, args ...any) (int64, error) {
	ct, err := r.db.q(ctx).Exec(ctx, sql, args...)
	if err != nil {
		return 0, mapError(err)
	}
	return ct.RowsAffected(), nil
}
