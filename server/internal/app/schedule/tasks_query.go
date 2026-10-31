package schedule

// 任务只读检索用例：运行页的列表与 CSV 导出共用一条读取路径。
//
// # 功能
// List 按作用域与筛选返回一页任务；ExportAll 以导出上限取一批行并
// 报告是否截断。本文件**只读**：不改任何行，不触发窗口发现。
//
// # 归属
// app/schedule 用例层。端口 TaskSource 实现为 pg.ScheduleRepo。
// 入口层是 entry/http/schedule.TasksHandler（JSON 与 CSV 两个 handler）。
//
// # 口径
// 作用域只收 master 与 admin：user 范围没有任务可见面，直接拒绝，
// 不把空集合当「自己的任务」。admin 空绑定就是空页，不是全量。
// day 缺省＝业务日今天（Clock 与 Location 由装配注入）；形状必须是
// YYYY-MM-DD，否则 400。state 必须在 core/status 的五个词内，否则 400。
// 列表的页大小缺省 50 由入口层经 app.PageLimit 定型；本用例判
// 1..200，越界 400。导出没有页大小入参，批量恒为 ExportCap。
// q 按手机号前缀或学校名子串匹配，LIKE 元字符由适配层转义。
// 排序固定 run_at 倒序、行 id 兜底；游标是不透明键集串。
// **手机号只出掩码**：行内明文在映射成 TaskView 时经 core/mask.MaskPhone，
// result 经 core/mask.MaskText；明文不出本包。

import (
	"context"
	"time"

	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coremask "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
)

// TaskSource 是任务只读检索端口（实现为 *pg.ScheduleRepo）。
// 返回的行含明文手机号：调用方只允许是用例层，出口前必须掩码。
type TaskSource interface {
	ListTasks(ctx context.Context, scope corescope.Scope, f coreschedule.TaskQueryFilter) ([]coreschedule.TaskQueryRow, string, error)
}

// ExportCap 是 CSV 导出的行数上限。超过即截断并由入口层显式标记。
// 值的唯一事实在 core/schedule.TaskExportCap。
const ExportCap = coreschedule.TaskExportCap

// TaskView 是给入口层的行：手机号已掩码，result 已过自由文本掩码。
type TaskView struct {
	ID         string
	AccountID  string
	PhoneMasked string
	SchoolID   string
	SchoolName string
	TaskName   string
	State      string
	Attempts   int
	Result     string
	RunAt      time.Time
	ClaimedAt  *time.Time
	UpdatedAt  time.Time
}

// TaskPage 是一页任务视图。NextCursor 空串表示末页；
// 导出路径里非空表示超过 ExportCap 被截断。
type TaskPage struct {
	Items      []TaskView
	NextCursor string
}

// TaskQuery 是任务只读检索用例。
type TaskQuery struct {
	tasks TaskSource
	clock coreclock.Clock
	loc   *time.Location
}

// NewTaskQuery 构造用例。Clock 为空时用系统墙钟；Location 为空时按 UTC。
func NewTaskQuery(tasks TaskSource, clk coreclock.Clock, loc *time.Location) *TaskQuery {
	if clk == nil {
		clk = coreclock.System{}
	}
	if loc == nil {
		loc = time.UTC
	}
	return &TaskQuery{tasks: tasks, clock: clk, loc: loc}
}

// List 返回一页任务视图。页大小 1..200：缺省 50 已由入口层定型，
// 显式越界在这里回 400。
func (q *TaskQuery) List(ctx context.Context, scope corescope.Scope, in coreschedule.TaskQueryFilter) (TaskPage, error) {
	filter, err := q.prepare(scope, in)
	if err != nil {
		return TaskPage{}, err
	}
	if filter.Limit < 1 || filter.Limit > 200 {
		return TaskPage{}, coreerrs.New(coreerrs.CodeValidation, "limit must be between 1 and 200")
	}
	rows, next, err := q.tasks.ListTasks(ctx, scope, *filter)
	if err != nil {
		return TaskPage{}, err
	}
	return TaskPage{Items: maskRows(rows), NextCursor: next}, nil
}

// ExportAll 以 ExportCap 取一批行。truncated 报告是否还有更多行被截掉。
// 导出没有 limit 入参：入口层不传页大小，这里直接套上限。
func (q *TaskQuery) ExportAll(ctx context.Context, scope corescope.Scope, in coreschedule.TaskQueryFilter) ([]TaskView, bool, error) {
	filter, err := q.prepare(scope, in)
	if err != nil {
		return nil, false, err
	}
	// 适配层按 limit+1 取行：next 非空即还有第 10001 行，就是截断。
	filter.Limit = ExportCap
	rows, next, err := q.tasks.ListTasks(ctx, scope, *filter)
	if err != nil {
		return nil, false, err
	}
	return maskRows(rows), next != "", nil
}

// prepare 校验作用域、业务日与状态词。页大小由两个用例各自判：
// 列表是契约的 1..200，导出是 ExportCap，两套上界不能混。
func (q *TaskQuery) prepare(scope corescope.Scope, in coreschedule.TaskQueryFilter) (*coreschedule.TaskQueryFilter, error) {
	switch scope.Kind() {
	case corescope.KindMaster, corescope.KindAdmin:
	default:
		return nil, coreerrs.New(coreerrs.CodeForbidden, "role cannot read tasks")
	}
	filter := in // 副本：不改调用方的入参。
	if filter.Day == "" {
		filter.Day = q.today()
	} else if !isBusinessDay(filter.Day) {
		return nil, coreerrs.New(coreerrs.CodeValidation, "day must be YYYY-MM-DD")
	}
	if filter.State != "" && !status.TaskState(filter.State).Valid() {
		return nil, coreerrs.New(coreerrs.CodeValidation, "unknown task state")
	}
	return &filter, nil
}

// today 返回注入时区的业务日。
func (q *TaskQuery) today() string {
	return q.clock.Now().In(q.loc).Format("2006-01-02")
}

// isBusinessDay 判定 day 是否为合法的 YYYY-MM-DD 业务日。
// 固定按 UTC 解析：日期没有时区语义，只验形状与存在性（2 月 30 日为假）。
func isBusinessDay(day string) bool {
	_, err := time.Parse("2006-01-02", day)
	return err == nil
}

// maskRows 把仓储行映射成视图并掩码。明文不出本函数。
func maskRows(rows []coreschedule.TaskQueryRow) []TaskView {
	items := make([]TaskView, len(rows))
	for i, r := range rows {
		items[i] = TaskView{
			ID: r.ID, AccountID: r.AccountID,
			PhoneMasked: coremask.MaskPhone(r.Phone),
			SchoolID:    r.SchoolID, SchoolName: r.SchoolName, TaskName: r.TaskName,
			State: r.State, Attempts: r.Attempts,
			Result: coremask.MaskText(r.Result),
			RunAt:  r.RunAt, ClaimedAt: r.ClaimedAt, UpdatedAt: r.UpdatedAt,
		}
	}
	return items
}
