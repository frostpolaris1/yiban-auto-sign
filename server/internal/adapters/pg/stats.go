package pg

// 看板聚合的 PostgreSQL 适配器。
//
// # 功能
// 按作用域读签到终态日/状态计数，以及某校自选时间片人数。
// 本文件**不**补缺失日期，**不**算窗口：两者归 app/stats 与 app/schedule。
//
// # 归属
// adapters 层。app/stats 只依赖 Store 端口，不见本类型。
//
// # 口径
// 日桶来自 sign_tasks.state，每账号每日一行；已软删账号不入选。
// by_status 用 DISTINCT 账号数，桶间可重叠。
// 自选人数按用户去重：同一校多个账号只计一次。

import (
	"context"
	"fmt"

	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
)

// StatsRepo 读看板聚合。
type StatsRepo struct {
	db *DB
}

// NewStatsRepo 构造聚合仓储。
func NewStatsRepo(db *DB) *StatsRepo {
	return &StatsRepo{db: db}
}

// SignStats 读 [from, to] 的日/状态计数与窗口去重值。
// 只返回库里出现过的日期；调用方补缺失日期。
//
// 同一账号同日在多个任务里出现时，**只取最后更新的一条**：
// 唯一键含 school_task_id，一校多任务会留下多行；日桶必须按账号收敛。
// 同口径还出现在 adapters/pg/calendar.go、app/calendar.latestByAccountDay
// 与 adapters/pg/engine.go 的当日终态；
// tie-break 是 updated_at DESC, id DESC，四处必须同改。
// updated_at 是通用 set_updated_at() 触发器时间（任意 UPDATE 都会刷新），
// 不是「状态最后变更」时刻——本口径取的是「最后更新」。
func (r *StatsRepo) SignStats(ctx context.Context, scope corescope.Scope, from, to string) (coreschedule.SignStats, error) {
	scopeSQL, scopeArgs, err := scopePredicate(scope, 3, "a.")
	if err != nil {
		return coreschedule.SignStats{}, err
	}
	args := append([]any{from, to}, scopeArgs...)
	picked := `
		WITH picked AS (
			SELECT DISTINCT ON (t.account_id, t.day)
			       t.account_id, t.day, t.state
			  FROM sign_tasks t
			  JOIN accounts a ON a.id = t.account_id
			 WHERE t.day >= $1::date AND t.day <= $2::date
			   AND a.deleted_at IS NULL` + scopeSQL + `
			 ORDER BY t.account_id, t.day, t.updated_at DESC, t.id DESC
		)`

	byDayRows, err := r.db.q(ctx).Query(ctx,
		picked+`
		SELECT picked.day::text, picked.state::text, count(*)::int
		  FROM picked
		 GROUP BY picked.day, picked.state
		 ORDER BY picked.day, picked.state`, args...)
	if err != nil {
		return coreschedule.SignStats{}, mapError(err)
	}
	var out coreschedule.SignStats
	// 一天多状态合并成一行：查询按 (day, state) 出行，契约一天一行。
	index := make(map[string]int)
	for byDayRows.Next() {
		var day, state string
		var accounts int
		if err := byDayRows.Scan(&day, &state, &accounts); err != nil {
			byDayRows.Close()
			return coreschedule.SignStats{}, mapError(err)
		}
		at, ok := index[day]
		if !ok {
			out.Days = append(out.Days, coreschedule.SignStatDay{Day: day})
			at = len(out.Days) - 1
			index[day] = at
		}
		if err := addStateCount(&out.Days[at], state, accounts); err != nil {
			byDayRows.Close()
			return coreschedule.SignStats{}, err
		}
	}
	if err := byDayRows.Err(); err != nil {
		byDayRows.Close()
		return coreschedule.SignStats{}, mapError(err)
	}
	byDayRows.Close()

	row := r.db.q(ctx).QueryRow(ctx,
		picked+`
		SELECT count(DISTINCT picked.account_id)::int,
		       count(DISTINCT picked.account_id) FILTER (WHERE picked.state = 'pending')::int,
		       count(DISTINCT picked.account_id) FILTER (WHERE picked.state = 'running')::int,
		       count(DISTINCT picked.account_id) FILTER (WHERE picked.state = 'success')::int,
		       count(DISTINCT picked.account_id) FILTER (WHERE picked.state = 'failed')::int,
		       count(DISTINCT picked.account_id) FILTER (WHERE picked.state = 'skipped')::int
		  FROM picked`, args...)
	if err := row.Scan(&out.Total,
		&out.ByStatus.Pending, &out.ByStatus.Running, &out.ByStatus.Success,
		&out.ByStatus.Failed, &out.ByStatus.Skipped); err != nil {
		return coreschedule.SignStats{}, mapError(err)
	}
	return out, nil
}

// OffsetCounts 读该校有 active 账号的用户自选偏移分布。
// 同一用户多个账号只计一次。待审、已拒与暂停账号不计入：
// 它们不参与签到，拥挤度必须与引擎的领取面同源。
func (r *StatsRepo) OffsetCounts(ctx context.Context, schoolID string) (map[int]int, error) {
	rows, err := r.db.q(ctx).Query(ctx, `
		SELECT p.offset_min, count(DISTINCT p.user_id)::int
		  FROM time_prefs p
		  JOIN accounts a ON a.owner_user_id = p.user_id
		 WHERE a.school_id = $1::uuid
		   AND a.deleted_at IS NULL
		   AND a.status = 'active'::account_status
		   AND p.offset_min IS NOT NULL
		 GROUP BY p.offset_min
		 ORDER BY p.offset_min`, schoolID)
	if err != nil {
		return nil, mapError(err)
	}
	defer rows.Close()
	out := make(map[int]int)
	for rows.Next() {
		var offset, users int
		if err := rows.Scan(&offset, &users); err != nil {
			return nil, mapError(err)
		}
		out[offset] = users
	}
	if err := rows.Err(); err != nil {
		return nil, mapError(err)
	}
	return out, nil
}

// addStateCount 把一行状态计数写进日桶。
// 未知状态是枚举漂移，必须显性失败，不能静默丢计数。
func addStateCount(row *coreschedule.SignStatDay, state string, accounts int) error {
	switch state {
	case string(corestatus.TaskPending):
		row.Pending = accounts
	case string(corestatus.TaskRunning):
		row.Running = accounts
	case string(corestatus.TaskSuccess):
		row.Success = accounts
	case string(corestatus.TaskFailed):
		row.Failed = accounts
	case string(corestatus.TaskSkipped):
		row.Skipped = accounts
	default:
		return fmt.Errorf("pg: unknown sign task state %q", state)
	}
	return nil
}
