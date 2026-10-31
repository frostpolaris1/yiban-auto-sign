package schedule

// 任务只读检索的数据形状。
//
// # 功能
// 定义运行页任务检索的入参与行形状。只读：本文件没有任何写路径。
//
// # 归属
// core/schedule 纯领域包。端口在 app/schedule，实现为 pg.ScheduleRepo；
// 形状放在 core 与仓库先例一致（AccountRef、WindowRecord 同款）：
// 适配器不 import app。
//
// # 口径
// TaskQueryRow.Phone 是**明文**手机号，只允许在 app 层的用例里出现，
// 出用例前必须经 core/mask 掩码；入口层与日志只见掩码值。
// State 是 core/status.TaskState 的字符串值；用例层校验词表。
// Day 是业务日 YYYY-MM-DD；用例层校验形状。

import "time"

// TaskExportCap 是 CSV 导出的行数上限（契约「capped at 10000 rows」）。
// 检索端口允许的最大页大小即此值：适配层用它做上界，
// 用例层把列表页大小（1..200）与导出批量分开判。
const TaskExportCap = 10000

// TaskQueryFilter 是任务只读检索的入参。
// 零值字段表示该维不过滤；Day 空由用例层补业务日今天。
type TaskQueryFilter struct {
	Day      string
	State    string
	SchoolID string
	Query    string
	Cursor   string
	Limit    int
}

// TaskQueryRow 是一行任务检索结果。
// Phone 是明文：出用例前必须掩码（见文件头口径）。
type TaskQueryRow struct {
	ID         string
	AccountID  string
	Phone      string
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
