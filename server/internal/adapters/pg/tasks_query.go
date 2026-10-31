package pg

// 本文件实现运行页的任务只读检索（app/schedule.TaskSource）。
//
// # 口径
// 作用域谓词在 school_tasks 上：sign_tasks 没有 school_id 列，
// 院校面经 JOIN school_tasks 取得。master 无谓词；admin 用
// scopePredicate 的 admin 分支（空绑定＝空页，绝不退化成全量）；
// user 范围在用例层已拒绝，这里再守一道。
// q 按手机号前缀或学校名子串匹配；LIKE 的三个元字符转义（同 users 列表）。
// 游标是 (run_at, id) 的键集串，与 appendKeyset 一一对应；
// 排序固定 run_at DESC、id DESC，多取一行判末页。

import (
	"context"
	"fmt"
	"time"

	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// taskQueryColumns 是任务检索行的选择列。顺序与 TaskQueryRow 扫描一致。
const taskQueryColumns = `s.id::text, s.account_id::text, a.phone,
	st.school_id::text, sc.name, st.name,
	s.state::text, s.attempts, s.result, s.run_at, s.claimed_at, s.updated_at`

// ListTasks 按作用域与筛选列签到任务，键集游标分页。
// 返回的行含明文手机号：只有 app/schedule 的用例允许消费。
func (r *ScheduleRepo) ListTasks(ctx context.Context, scope corescope.Scope,
	f coreschedule.TaskQueryFilter) ([]coreschedule.TaskQueryRow, string, error) {
	if f.Limit < 1 || f.Limit > coreschedule.TaskExportCap {
		return nil, "", validationError("limit must be between 1 and the export cap")
	}
	if scope.Kind() != corescope.KindMaster && scope.Kind() != corescope.KindAdmin {
		return nil, "", validationError("tasks are scoped to master and admin")
	}

	where := ` FROM sign_tasks s
	JOIN school_tasks st ON st.id = s.school_task_id
	JOIN schools sc ON sc.id = st.school_id
	JOIN accounts a ON a.id = s.account_id
	WHERE TRUE`
	var args []any

	// 作用域先于一切筛选：admin 的绑定集合是硬边界。
	// 谓词作用在 st（school_tasks 的 school_id 列）上。
	scopeClause, scopeArgs, err := scopePredicate(scope, len(args)+1, "st.")
	if err != nil {
		return nil, "", err
	}
	args = append(args, scopeArgs...)
	where += scopeClause

	if f.Day != "" {
		args = append(args, f.Day)
		where += fmt.Sprintf(" AND s.day = $%d::date", len(args))
	}
	if f.State != "" {
		args = append(args, f.State)
		where += fmt.Sprintf(" AND s.state = $%d::sign_task_state", len(args))
	}
	if f.SchoolID != "" {
		args = append(args, f.SchoolID)
		where += fmt.Sprintf(" AND st.school_id = $%d::uuid", len(args))
	}
	if f.Query != "" {
		args = append(args, escapeLikePrefix(f.Query))
		// 手机号是前缀匹配；学校名是子串匹配。元字符已转义，按字面处理。
		where += fmt.Sprintf(
			` AND (a.phone LIKE $%d || '%%' ESCAPE '\'`+
				` OR sc.name LIKE '%%' || $%d || '%%' ESCAPE '\')`, len(args), len(args))
	}

	sqlText, args, err := appendKeyset("SELECT "+taskQueryColumns+where,
		args, f.Cursor, "s.run_at", "s.id")
	if err != nil {
		return nil, "", err
	}
	// 排序列用限定名：与 users 列表同因，输出列名不参与 ORDER BY 解析。
	sqlText += " ORDER BY s.run_at DESC, s.id DESC"
	sqlText, args = appendLimit(sqlText, args, f.Limit)

	rows, err := r.db.q(ctx).Query(ctx, sqlText, args...)
	if err != nil {
		return nil, "", mapError(err)
	}
	defer rows.Close()

	var out []coreschedule.TaskQueryRow
	for rows.Next() {
		var row coreschedule.TaskQueryRow
		if err := rows.Scan(&row.ID, &row.AccountID, &row.Phone,
			&row.SchoolID, &row.SchoolName, &row.TaskName,
			&row.State, &row.Attempts, &row.Result,
			&row.RunAt, &row.ClaimedAt, &row.UpdatedAt); err != nil {
			return nil, "", mapError(err)
		}
		out = append(out, row)
	}
	if err := rows.Err(); err != nil {
		return nil, "", mapError(err)
	}
	// 多取的那一行只用于判末页：不进结果，不进游标。
	more := false
	if len(out) > f.Limit {
		more = true
		out = out[:f.Limit]
	}
	next := nextCursor(out, more, func(row coreschedule.TaskQueryRow) (time.Time, string) {
		return row.RunAt, row.ID
	})
	return out, next, nil
}
