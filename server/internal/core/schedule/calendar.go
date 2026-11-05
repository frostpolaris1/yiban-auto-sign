package schedule

import "time"

// CalendarUnknown 是日历读面对 pending、running 与未知取值的归并桶。
// 终态词表仍以 core/status 为唯一来源；本常量只表示"没有当日结论"。
const CalendarUnknown = "unknown"

// CalendarTaskRow 是日历聚合的只读任务行。
// 一行对应一个 sign_tasks 行；同账号同日多行由用例层按 UpdatedAt 取最近一行。
type CalendarTaskRow struct {
	AccountID string
	Day       string // YYYY-MM-DD，业务日
	State     string // sign_task_state 的文本值
	RunAt     time.Time
	Attempts  int
	UpdatedAt time.Time
}

// CalendarDayCounts 是一个业务日的账号级计数。
// Total 恒为四个桶之和；Future 为真时计数全零。
type CalendarDayCounts struct {
	Day     string
	Total   int
	Success int
	Failed  int
	Skipped int
	Unknown int
	Future  bool
}

// CalendarAccountOutcome 是一个账号在某个业务日的最近一行结果。
// RunAt 为 nil 表示该行没有计划时刻。
// v1 不提供 reason/suggestion：sign_tasks 只有 result，不把上游原文映射进响应。
type CalendarAccountOutcome struct {
	AccountID string
	State     string // success / failed / skipped / unknown
	RunAt     *time.Time
	Attempts  int
}

// CalendarRange 是 GET /api/me/sign-calendar 的读模型。
// UpdatedAt 是范围内任务行的最大更新时间；范围内无行时取服务端当前时刻。
type CalendarRange struct {
	Timezone  string
	Today     string
	UpdatedAt time.Time
	Days      []CalendarDayCounts
}

// CalendarDayDetail 是 GET /api/me/sign-calendar/{day} 的读模型。
// Future 为真时 Accounts 为空；与范围读面的未来日清零同口径。
type CalendarDayDetail struct {
	Day      string
	Timezone string
	Accounts []CalendarAccountOutcome
	Future   bool
}
