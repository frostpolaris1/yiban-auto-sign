// DiagnosticRepo 是诊断读模型的只读仓储（P6）。
//
// # 功能
// 当日任务行、当日窗口缓存与账号出口标签的批量只读查询；
// 按作用域的当日状态计数（SSE snapshot 用）。
// 账号集合先经作用域过滤，这里按显式 id 集查询，不重复判可见面。
//
// # 归属
// adapters/pg。端口见 app/diagnostic.Store。
//
// # 口径
// 全部只读；day 是业务日 YYYY-MM-DD，按库列 date 比较。
// StateCounts 只数未软删账号：软删账号的遗留行不进管理员前景。
package pg

import (
	"context"

	"github.com/jackc/pgx/v5"

	appdiagnostic "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/diagnostic"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// DiagnosticRepo 是诊断只读仓储。
type DiagnosticRepo struct {
	db *DB
}

// NewDiagnosticRepo 构造诊断仓储。
func NewDiagnosticRepo(db *DB) *DiagnosticRepo { return &DiagnosticRepo{db: db} }

// TodayTasks 读账号集合在业务日的全部任务行。
func (r *DiagnosticRepo) TodayTasks(ctx context.Context, accountIDs []string, day string) ([]appdiagnostic.TaskRow, error) {
	rows, err := r.db.q(ctx).Query(ctx,
		`SELECT id::text, school_task_id::text, account_id::text, state::text,
		        result, attempts, run_at, updated_at
		 FROM sign_tasks
		 WHERE day = $1::date AND account_id = ANY($2::text[]::uuid[])`,
		day, accountIDs)
	if err != nil {
		return nil, mapError(err)
	}
	defer rows.Close()
	return scanDiagnosticTasks(rows)
}

// Windows 读任务集合在业务日的窗口缓存；无行不报错。
func (r *DiagnosticRepo) Windows(ctx context.Context, schoolTaskIDs []string, day string) (map[string]appdiagnostic.WindowInfo, error) {
	rows, err := r.db.q(ctx).Query(ctx,
		`SELECT school_task_id::text, start_at, end_at, source, version
		 FROM window_cache
		 WHERE day = $1::date AND school_task_id = ANY($2::text[]::uuid[])`,
		day, schoolTaskIDs)
	if err != nil {
		return nil, mapError(err)
	}
	defer rows.Close()
	out := map[string]appdiagnostic.WindowInfo{}
	for rows.Next() {
		var id string
		var w appdiagnostic.WindowInfo
		if err := rows.Scan(&id, &w.Start, &w.End, &w.Source, &w.Version); err != nil {
			return nil, mapError(err)
		}
		out[id] = w
	}
	return out, mapError(rows.Err())
}

// Egresses 读账号集合的粘性出口标签；未绑定账号缺席。
func (r *DiagnosticRepo) Egresses(ctx context.Context, accountIDs []string) (map[string]appdiagnostic.EgressInfo, error) {
	rows, err := r.db.q(ctx).Query(ctx,
		`SELECT ae.account_id::text, e.identity
		 FROM account_egress ae
		 JOIN egresses e ON e.id = ae.egress_id
		 WHERE ae.account_id = ANY($1::text[]::uuid[])`,
		accountIDs)
	if err != nil {
		return nil, mapError(err)
	}
	defer rows.Close()
	out := map[string]appdiagnostic.EgressInfo{}
	for rows.Next() {
		var id, identity string
		if err := rows.Scan(&id, &identity); err != nil {
			return nil, mapError(err)
		}
		out[id] = appdiagnostic.EgressInfo{Identity: identity}
	}
	return out, mapError(rows.Err())
}

// StateCounts 按作用域统计业务日的任务状态计数。
// master 全量；admin 只数绑定校；user 只数自己的账号。
func (r *DiagnosticRepo) StateCounts(ctx context.Context, scope corescope.Scope, day string) (map[string]int, error) {
	clause, args, err := scopePredicate(scope, 2, "a.")
	if err != nil {
		return nil, err
	}
	query := `SELECT st.state::text, count(*)
	          FROM sign_tasks st
	          JOIN accounts a ON a.id = st.account_id
	          WHERE st.day = $1::date AND a.deleted_at IS NULL` + clause + `
	          GROUP BY st.state`
	rows, err := r.db.q(ctx).Query(ctx, query, append([]any{day}, args...)...)
	if err != nil {
		return nil, mapError(err)
	}
	defer rows.Close()
	out := map[string]int{}
	for rows.Next() {
		var state string
		var n int
		if err := rows.Scan(&state, &n); err != nil {
			return nil, mapError(err)
		}
		out[state] = n
	}
	return out, mapError(rows.Err())
}

func scanDiagnosticTasks(rows pgx.Rows) ([]appdiagnostic.TaskRow, error) {
	var out []appdiagnostic.TaskRow
	for rows.Next() {
		var t appdiagnostic.TaskRow
		if err := rows.Scan(&t.TaskID, &t.SchoolTaskID, &t.AccountID, &t.State,
			&t.Result, &t.Attempts, &t.RunAt, &t.UpdatedAt); err != nil {
			return nil, mapError(err)
		}
		out = append(out, t)
	}
	return out, mapError(rows.Err())
}
