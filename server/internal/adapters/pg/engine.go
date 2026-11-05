package pg

// 本文件实现执行监控快照（app/schedule.EngineSource）。
//
// # 口径
// 作用域谓词在 school_tasks 上：sign_tasks 没有 school_id 列，
// 院校面经 JOIN school_tasks 取得。master 无谓词；admin 用
// scopePredicate 的 admin 分支（空绑定＝空快照，绝不退化成全量）；
// user 范围在用例层已拒绝，这里再守一道。
// 四段查询共享同一份 (scope, day, now)：
//   - 在飞：running 且认领与租约都已写；
//   - 到期：pending 且 run_at <= now（如实反映「到点未执行」，
//     不再判账号可用性——那是认领判据，不是监控口径）；
//   - 当日终态：每账号只看当日作用域内最后一条任务行
//     （tie-break updated_at DESC, id DESC，与 stats/calendar 同源），
//     该行 success 计一账号、failed 计一账号，其余状态不进两者，
//     软删账号整段排除——看板 KPI 与 sign-stats 同口径；
//   - 待重试：pending 且 attempts>0，按 run_at 升序，
//     行数截断于 EngineRetryLimit（多取一行探测截断）。
// 列表顺序固定（在飞按认领时刻、待重试按落点），行 id 兜底，可重放。

import (
	"context"
	"strconv"
	"time"

	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// engineFrom 是三段查询共享的 JOIN 骨架与作用域占位
// （在飞/到期/待重试；当日终态自建 FROM：无需 schools，
// 另带软删过滤）。作用域谓词作用在 st（school_tasks 的 school_id 列）上。
const engineFrom = ` FROM sign_tasks s
JOIN school_tasks st ON st.id = s.school_task_id
JOIN schools sc ON sc.id = st.school_id
JOIN accounts a ON a.id = s.account_id
WHERE TRUE`

// EngineSnapshot 按作用域读取单进程执行监控快照。
// 返回的行含明文手机号：只有 app/schedule 的用例允许消费。
func (r *ScheduleRepo) EngineSnapshot(ctx context.Context, scope corescope.Scope,
	day string, now time.Time) (coreschedule.EngineData, error) {
	if scope.Kind() != corescope.KindMaster && scope.Kind() != corescope.KindAdmin {
		return coreschedule.EngineData{}, validationError("engine snapshot is scoped to master and admin")
	}
	data := coreschedule.EngineData{}

	// 列表查询无其他参数：作用域谓词占 $1。
	// 空绑定给空数组，永不退化成全量。
	scopeClause, scopeArgs, err := scopePredicate(scope, 1, "st.")
	if err != nil {
		return coreschedule.EngineData{}, err
	}

	inflight := `SELECT s.account_id::text, a.phone, st.school_id::text, sc.name,
		s.claimed_at, s.lease_expires_at` + engineFrom + `
		AND s.state = 'running' AND s.claimed_at IS NOT NULL
		AND s.lease_expires_at IS NOT NULL` + scopeClause + `
		ORDER BY s.claimed_at, s.id`
	if data.Inflight, err = r.engineInflight(ctx, inflight, scopeArgs); err != nil {
		return coreschedule.EngineData{}, err
	}

	// 到期计数保持原口径：pending 且 run_at<=now 的行数。
	// $1 是 now：作用域谓词占 $2。
	dueScopeClause, dueScopeArgs, err := scopePredicate(scope, 2, "st.")
	if err != nil {
		return coreschedule.EngineData{}, err
	}
	due := `SELECT count(*) FILTER (WHERE s.state = 'pending' AND s.run_at <= $1)` +
		engineFrom + dueScopeClause
	dueArgs := append([]any{now}, dueScopeArgs...)
	if err := r.db.q(ctx).QueryRow(ctx, due, dueArgs...).Scan(&data.DueNow); err != nil {
		return coreschedule.EngineData{}, mapError(err)
	}

	// 当日终态按账号去重：每账号只取当日作用域内最后一条任务行
	// （DISTINCT ON，tie-break updated_at DESC, id DESC，与
	// StatsRepo.SignStats 及 calendar 同源），该行 success 计一账号、
	// failed 计一账号；skipped/pending/running 都不进两者；
	// 软删账号整段排除。注意 updated_at 是通用 set_updated_at()
	// 触发器时间（BEFORE UPDATE，任意 UPDATE 都会刷新），
	// 不是「状态最后变更」时刻——同源口径取的都是「最后更新」。
	// 作用域谓词仍打 st.（$2）：
	// admin 空绑定给空集，绝不退化成全量。
	termScopeClause, termScopeArgs, err := scopePredicate(scope, 2, "st.")
	if err != nil {
		return coreschedule.EngineData{}, err
	}
	terminal := `WITH latest AS (
		SELECT DISTINCT ON (s.account_id) s.state
		  FROM sign_tasks s
		  JOIN school_tasks st ON st.id = s.school_task_id
		  JOIN accounts a ON a.id = s.account_id
		 WHERE s.day = $1::date
		   AND a.deleted_at IS NULL` + termScopeClause + `
		 ORDER BY s.account_id, s.updated_at DESC, s.id DESC)
	SELECT count(*) FILTER (WHERE latest.state = 'success'),
	       count(*) FILTER (WHERE latest.state = 'failed')
	  FROM latest`
	termArgs := append([]any{day}, termScopeArgs...)
	if err := r.db.q(ctx).QueryRow(ctx, terminal, termArgs...).
		Scan(&data.TodaySuccess, &data.TodayFailed); err != nil {
		return coreschedule.EngineData{}, mapError(err)
	}

	// LIMIT 的参数位跟在作用域参数之后：master 无谓词时占 $1，
	// admin 的作用域占 $1 时占 $2（与 ListTasks 的参数位推法同源）。
	limitParam := len(scopeArgs) + 1
	retrying := `SELECT s.id::text, s.account_id::text, a.phone, st.school_id::text, sc.name,
		st.name, s.attempts, s.run_at, s.result` + engineFrom + `
		AND s.state = 'pending' AND s.attempts > 0` + scopeClause + `
		ORDER BY s.run_at, s.id
		LIMIT $` + strconv.Itoa(limitParam)
	retryArgs := append(append([]any{}, scopeArgs...), coreschedule.EngineRetryLimit+1)
	retryingRows, truncated, err := r.engineRetrying(ctx, retrying, retryArgs)
	if err != nil {
		return coreschedule.EngineData{}, err
	}
	data.Retrying = retryingRows
	// 多取的一行只用于报截断：截断事实随响应显式上报，不静默。
	data.RetryingTruncated = truncated
	return data, nil
}

// engineInflight 扫在飞行。$1 是作用域参数（空绑定给空数组，永不退化成全量）。
func (r *ScheduleRepo) engineInflight(ctx context.Context, query string, scopeArgs []any) ([]coreschedule.EngineInflightRow, error) {
	args := append([]any{}, scopeArgs...)
	rows, err := r.db.q(ctx).Query(ctx, query, args...)
	if err != nil {
		return nil, mapError(err)
	}
	defer rows.Close()
	var out []coreschedule.EngineInflightRow
	for rows.Next() {
		var row coreschedule.EngineInflightRow
		if err := rows.Scan(&row.AccountID, &row.Phone, &row.SchoolID, &row.SchoolName,
			&row.ClaimedAt, &row.LeaseExpiresAt); err != nil {
			return nil, mapError(err)
		}
		out = append(out, row)
	}
	return out, mapError(rows.Err())
}

// engineRetrying 扫待重试行。$1 是作用域参数，$2 是上限+1：
// 返回行数超过上限即截断到上限，第二返回值报告截断事实。
func (r *ScheduleRepo) engineRetrying(ctx context.Context, query string, args []any) ([]coreschedule.EngineRetryRow, bool, error) {
	rows, err := r.db.q(ctx).Query(ctx, query, args...)
	if err != nil {
		return nil, false, mapError(err)
	}
	defer rows.Close()
	var out []coreschedule.EngineRetryRow
	for rows.Next() {
		var row coreschedule.EngineRetryRow
		if err := rows.Scan(&row.TaskID, &row.AccountID, &row.Phone, &row.SchoolID, &row.SchoolName,
			&row.TaskName, &row.Attempts, &row.RunAt, &row.Result); err != nil {
			return nil, false, mapError(err)
		}
		out = append(out, row)
	}
	if err := rows.Err(); err != nil {
		return nil, false, mapError(err)
	}
	if len(out) > coreschedule.EngineRetryLimit {
		return out[:coreschedule.EngineRetryLimit], true, nil
	}
	return out, false, nil
}
