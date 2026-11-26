package pg

// 本文件实现执行监控快照（app/schedule.EngineSource）。
//
// # 口径
// 作用域谓词在 school_tasks 上：sign_tasks 没有 school_id 列，
// 院校面经 JOIN school_tasks 取得。master 无谓词；admin 用
// scopePredicate 的 admin 分支（空绑定＝空快照，绝不退化成全量）；
// user 范围在用例层已拒绝，这里再守一道。
// 每次快照固定两次往返，共享同一份 (scope, day, now)：
//   - 查询一（一次 QueryRow）取到期计数与当日终态两计数。scoped CTE
//     只建一次作用域面，due 标量子查询与 latest 去重共用同一谓词；
//     latest 按账号取当日作用域内最后一条任务行
//     （tie-break updated_at DESC, id DESC，与 stats/calendar 同源），
//     该行 success 计一账号、failed 计一账号，其余状态不进两者，
//     软删账号整段排除——看板 KPI 与 sign-stats 同口径；
//   - 查询二（一次 Query）取在飞与待重试行，两段经 UNION ALL 合并。
//     待重试段先在子查询里按 run_at, id 排序并 LIMIT 上限+1：LIMIT
//     只作用该段，多取的一行只用于探测截断。
// 在飞：running、认领与租约都已写且租约未过期（过期租约不是在飞）；
// 到期：pending 且 run_at <= now（如实反映「到点未执行」，
//      不再判账号可用性——那是认领判据，不是监控口径）；
// 待重试：pending 且 attempts>0，按 run_at 升序，行数截断于
//      EngineRetryLimit（多取一行探测截断）。
// 在飞/到期/待重试都只算 day >= 今天的行：跨日行不混算（P1-6/2-15）。
// scoped CTE 内把 day 谓词下推到 sign_tasks：三个消费者都只关心今天及
// 以后，不下推会让历史行走全表扫描（59 万行实测 800ms -> 7.5ms）。
// 列表顺序固定（在飞按认领时刻、待重试按落点），行 id 兜底，可重放；
// 外层排序把 id 显式转 uuid，不依赖库里对 text 的 collation。

import (
	"context"
	"database/sql"
	"fmt"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// engineFrom 是两次往返共享的 JOIN 骨架与作用域占位：
// 查询一的 scoped CTE、查询二的在飞段与待重试子查询都以它起头，
// 作用域谓词只在调用方拼一次，作用在 st（school_tasks 的 school_id 列）上。
const engineFrom = ` FROM sign_tasks s
JOIN school_tasks st ON st.id = s.school_task_id
JOIN schools sc ON sc.id = st.school_id
JOIN accounts a ON a.id = s.account_id
WHERE TRUE`

// EngineSnapshot 按作用域读取单进程执行监控快照。
// 返回的行含明文手机号：只有 app/schedule 的用例允许消费。
// 固定两次查询：计数一次（QueryRow），行一次（Query）。
func (r *ScheduleRepo) EngineSnapshot(ctx context.Context, scope corescope.Scope,
	day string, now time.Time) (coreschedule.EngineData, error) {
	if scope.Kind() != corescope.KindMaster && scope.Kind() != corescope.KindAdmin {
		return coreschedule.EngineData{}, validationError("engine snapshot is scoped to master and admin")
	}
	// 作用域谓词只算一次：两次查询的 $1=day、$2=now 位置固定，
	// 作用域参数都从 $3 起；查询二的 LIMIT 参数跟在作用域参数之后。
	scopeClause, scopeArgs, err := scopePredicate(scope, 3, "st.")
	if err != nil {
		return coreschedule.EngineData{}, err
	}
	data := coreschedule.EngineData{}

	// 查询一：到期计数与当日终态两计数，一次 QueryRow 取三个值。
	// scoped 是唯一的作用域面：due 标量子查询与 latest 去重共用它，
	// 作用域谓词不会因两处引用而漂移。
	countsSQL := `WITH scoped AS (
	SELECT s.state, s.run_at, s.day, s.account_id, s.updated_at, s.id,
	       a.deleted_at AS account_deleted_at` + engineFrom + scopeClause + `
	  AND s.day >= $1::date
), latest AS (
	SELECT DISTINCT ON (account_id) state
	  FROM scoped
	 WHERE day = $1::date
	   AND account_deleted_at IS NULL
	 ORDER BY account_id, updated_at DESC, id DESC)
SELECT (SELECT count(*) FROM scoped
         WHERE state = 'pending' AND run_at <= $2 AND day >= $1::date),
       count(*) FILTER (WHERE state = 'success'),
       count(*) FILTER (WHERE state = 'failed')
  FROM latest`
	countsArgs := append([]any{day, now}, scopeArgs...)
	if err := r.db.q(ctx).QueryRow(ctx, countsSQL, countsArgs...).
		Scan(&data.DueNow, &data.TodaySuccess, &data.TodayFailed); err != nil {
		return coreschedule.EngineData{}, mapError(err)
	}

	// 查询二：在飞与待重试行，一次 Query 取两段。
	// 两段列形一致，UNION ALL 后按 (kind, 排序时刻, id::uuid) 输出；
	// 待重试段包在子查询里，LIMIT 只截该段。
	retryLimitParam := len(scopeArgs) + 3
	rowsSQL := fmt.Sprintf(`SELECT kind, id, account_id, phone, school_id, school_name,
       task_name, attempts, run_at, result, claimed_at, lease_expires_at
FROM (
	SELECT 0 AS kind, s.id::text AS id, s.account_id::text AS account_id,
	       a.phone, st.school_id::text AS school_id, sc.name AS school_name,
	       NULL::text AS task_name, NULL::int AS attempts,
	       NULL::timestamptz AS run_at, NULL::text AS result,
	       s.claimed_at, s.lease_expires_at, s.claimed_at AS sort_at`+engineFrom+`
	  AND s.state = 'running' AND s.claimed_at IS NOT NULL
	  AND s.lease_expires_at IS NOT NULL AND s.lease_expires_at > $2
	  AND s.day >= $1::date`+scopeClause+`
	UNION ALL
	SELECT 1, r.id::text, r.account_id::text, r.phone, r.school_id::text, r.school_name,
	       r.task_name, r.attempts, r.run_at, r.result,
	       NULL::timestamptz, NULL::timestamptz, r.run_at
	  FROM (
		SELECT s.id, s.account_id, a.phone, st.school_id, sc.name AS school_name,
		       st.name AS task_name, s.attempts, s.run_at, s.result`+engineFrom+`
		  AND s.state = 'pending' AND s.attempts > 0 AND s.day >= $1::date`+scopeClause+`
		 ORDER BY s.run_at, s.id
		 LIMIT $%d
	  ) r
) x
ORDER BY kind, sort_at, id::uuid`, retryLimitParam)
	rowsArgs := append([]any{day, now}, scopeArgs...)
	rowsArgs = append(rowsArgs, coreschedule.EngineRetryLimit+1)
	rows, err := r.db.q(ctx).Query(ctx, rowsSQL, rowsArgs...)
	if err != nil {
		return coreschedule.EngineData{}, mapError(err)
	}
	defer rows.Close()

	var inflight []coreschedule.EngineInflightRow
	var retrying []coreschedule.EngineRetryRow
	for rows.Next() {
		var (
			kind                                       int
			id, accountID, phone, schoolID, schoolName string
			taskName, result                           sql.NullString
			attempts                                   sql.NullInt64
			runAt, claimedAt, leaseExpiresAt           sql.NullTime
		)
		if err := rows.Scan(&kind, &id, &accountID, &phone, &schoolID, &schoolName,
			&taskName, &attempts, &runAt, &result, &claimedAt, &leaseExpiresAt); err != nil {
			return coreschedule.EngineData{}, mapError(err)
		}
		switch kind {
		case 0:
			if !claimedAt.Valid || !leaseExpiresAt.Valid {
				return coreschedule.EngineData{}, errs.New(errs.CodeInternal,
					"engine snapshot: inflight row is missing claimed_at/lease_expires_at")
			}
			inflight = append(inflight, coreschedule.EngineInflightRow{
				AccountID:      accountID,
				Phone:          phone,
				SchoolID:       schoolID,
				SchoolName:     schoolName,
				ClaimedAt:      claimedAt.Time,
				LeaseExpiresAt: leaseExpiresAt.Time,
			})
		case 1:
			if !taskName.Valid || !attempts.Valid || !runAt.Valid || !result.Valid {
				return coreschedule.EngineData{}, errs.New(errs.CodeInternal,
					"engine snapshot: retry row is missing task fields")
			}
			retrying = append(retrying, coreschedule.EngineRetryRow{
				TaskID:     id,
				AccountID:  accountID,
				Phone:      phone,
				SchoolID:   schoolID,
				SchoolName: schoolName,
				TaskName:   taskName.String,
				Attempts:   int(attempts.Int64),
				RunAt:      runAt.Time,
				Result:     result.String,
			})
		default:
			return coreschedule.EngineData{}, errs.New(errs.CodeInternal,
				fmt.Sprintf("engine snapshot: unknown row kind %d", kind))
		}
	}
	if err := rows.Err(); err != nil {
		return coreschedule.EngineData{}, mapError(err)
	}

	data.Inflight = inflight
	// 多取的一行只用于报截断：截断事实随响应显式上报，不静默。
	if len(retrying) > coreschedule.EngineRetryLimit {
		retrying = retrying[:coreschedule.EngineRetryLimit]
		data.RetryingTruncated = true
	}
	data.Retrying = retrying
	return data, nil
}
