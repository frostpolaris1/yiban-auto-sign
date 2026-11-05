// CalendarRepo 是签到日历读模型的只读仓储。
//
// # 功能
// 按会话 userID 与业务日范围读取 sign_tasks 行，供 app/calendar 聚合。
// 本仓储**不**计数、**不**归一状态：去重与终态映射在用例层一次完成。
//
// # 归属
// adapters/pg。端口见 app/calendar.Store。
//
// # 口径
// 账号归属与软删过滤是 SQL 的硬谓词：调用方只传 userID，不能放宽范围。
// 状态列按文本返回，避免在 SQL 里复制状态词表；排序按
// (账号, 日期, 更新时间倒序, id 倒序)，用例层仍会再取一次最大值。
package pg

import (
	"context"

	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
)

// CalendarRepo 是签到日历只读仓储。
type CalendarRepo struct {
	db *DB
}

// NewCalendarRepo 构造日历仓储。
func NewCalendarRepo(db *DB) *CalendarRepo { return &CalendarRepo{db: db} }

// CalendarTasks 读 userID 名下未软删账号在 from..to（含首尾）的任务行。
// 同账号同日去重的 tie-break 与 stats.go 的 DISTINCT ON 同口径：
// updated_at DESC, id DESC；另有 app/calendar.latestByAccountDay 与
// adapters/pg/engine.go 的当日终态，同口径各处必须同改。
func (r *CalendarRepo) CalendarTasks(ctx context.Context, userID, from, to string) ([]coreschedule.CalendarTaskRow, error) {
	rows, err := r.db.q(ctx).Query(ctx,
		`SELECT st.account_id::text, st.day::text, st.state::text, st.run_at, st.attempts, st.updated_at
		   FROM sign_tasks st
		   JOIN accounts a ON a.id = st.account_id
		  WHERE a.owner_user_id = $1::uuid
		    AND a.deleted_at IS NULL
		    AND st.day >= $2::date AND st.day <= $3::date
		  ORDER BY st.account_id, st.day, st.updated_at DESC, st.id DESC`,
		userID, from, to)
	if err != nil {
		return nil, mapError(err)
	}
	defer rows.Close()
	var out []coreschedule.CalendarTaskRow
	for rows.Next() {
		var row coreschedule.CalendarTaskRow
		if err := rows.Scan(&row.AccountID, &row.Day, &row.State,
			&row.RunAt, &row.Attempts, &row.UpdatedAt); err != nil {
			return nil, mapError(err)
		}
		out = append(out, row)
	}
	return out, mapError(rows.Err())
}
