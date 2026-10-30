package pg

// 过期数据清理的仓储。只清三张表：会话、重置令牌、旧任务行。
//
// 审计链（audit_logs）不在这里清：它是 append-only，清理只能走迁移级
// 操作（重写 guard），否则保留期与不可篡改互相打架。见 core/audit 口径。

import (
	"context"
	"time"
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

// deleteBatch 执行一条按 id 子查询限定批量的 DELETE。
func (r *MaintenanceRepo) deleteBatch(ctx context.Context, sql string, args ...any) (int64, error) {
	ct, err := r.db.q(ctx).Exec(ctx, sql, args...)
	if err != nil {
		return 0, mapError(err)
	}
	return ct.RowsAffected(), nil
}
