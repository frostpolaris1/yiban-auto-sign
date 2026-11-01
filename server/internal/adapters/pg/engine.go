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
//   - 当日终态：success 与 failed 各一计数（FILTER 一次扫完）；
//   - 待重试：pending 且 attempts>0，按 run_at 升序。
// 列表顺序固定（在飞按认领时刻、待重试按落点），行 id 兜底，可重放。

import (
	"context"
	"time"

	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// engineFrom 是四段查询共享的 JOIN 骨架与作用域占位。
// 作用域谓词作用在 st（school_tasks 的 school_id 列）上。
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

	// 计数查询的 $1/$2 是 now 与 day：作用域谓词占 $3。
	countScopeClause, countScopeArgs, err := scopePredicate(scope, 3, "st.")
	if err != nil {
		return coreschedule.EngineData{}, err
	}
	counts := `SELECT
		  count(*) FILTER (WHERE s.state = 'pending' AND s.run_at <= $1),
		  count(*) FILTER (WHERE s.day = $2::date AND s.state = 'success'),
		  count(*) FILTER (WHERE s.day = $2::date AND s.state = 'failed')` + engineFrom + countScopeClause
	countArgs := append([]any{now, day}, countScopeArgs...)
	if err := r.db.q(ctx).QueryRow(ctx, counts, countArgs...).
		Scan(&data.DueNow, &data.TodaySuccess, &data.TodayFailed); err != nil {
		return coreschedule.EngineData{}, mapError(err)
	}

	retrying := `SELECT s.account_id::text, a.phone, st.school_id::text, sc.name,
		s.attempts, s.run_at, s.result` + engineFrom + `
		AND s.state = 'pending' AND s.attempts > 0` + scopeClause + `
		ORDER BY s.run_at, s.id`
	if data.Retrying, err = r.engineRetrying(ctx, retrying, scopeArgs); err != nil {
		return coreschedule.EngineData{}, err
	}
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

// engineRetrying 扫待重试行。$1 是作用域参数。
func (r *ScheduleRepo) engineRetrying(ctx context.Context, query string, scopeArgs []any) ([]coreschedule.EngineRetryRow, error) {
	args := append([]any{}, scopeArgs...)
	rows, err := r.db.q(ctx).Query(ctx, query, args...)
	if err != nil {
		return nil, mapError(err)
	}
	defer rows.Close()
	var out []coreschedule.EngineRetryRow
	for rows.Next() {
		var row coreschedule.EngineRetryRow
		if err := rows.Scan(&row.AccountID, &row.Phone, &row.SchoolID, &row.SchoolName,
			&row.Attempts, &row.RunAt, &row.Result); err != nil {
			return nil, mapError(err)
		}
		out = append(out, row)
	}
	return out, mapError(rows.Err())
}
