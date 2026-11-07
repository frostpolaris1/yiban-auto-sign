// Package schedule 定义排期的领域取值对象。
//
// # 功能
// 定义计划行（PlanRow）与窗口记录（WindowRecord）。
// 本包**不**排期、**不**读写库、**不**算几何——几何在 core/plan，
// 窗口谓词在 core/window。
//
// # 归属
// core 层纯领域包。app/schedule 声明端口，adapters/pg 实现。
//
// # 复用
// PlanRow、WindowRecord、DayOutcomeCounts、TimePref、State 转换、自选偏移的取值边界。
//
// # 口径
// 一行计划 = 一个账号 × 一个学校任务 × 一个业务日。
// 状态词表与 core/status 的 TaskState 同形（pending/running/
// success/failed/skipped），转换只在此一处。
package schedule

import "time"

// 自选偏移的取值范围。用户自选与旧库补录共用此边界。
// 建表 CHECK（migrations/postgres/00014）取同值；两侧不一致时数据库报错，不静默写入。
const (
	// MaxOffsetMin 是偏移上限（不含）：业务日最多 1440 分钟。
	MaxOffsetMin = 1440
	// GranularityMin 是偏移粒度，与计划片跨度一致。
	GranularityMin = 5

	// DefaultSignWindowStartMinute 是服务端默认签到窗口起点（06:30）。
	// 平台与缓存都不可用时，计划回退用它；用户自选读面也按它展示窗口。
	// 两处必须取同一对常量：复制一份就是第二个事实源。
	DefaultSignWindowStartMinute = 6*60 + 30
	// DefaultSignWindowEndMinute 是服务端默认签到窗口终点（07:50）。
	DefaultSignWindowEndMinute = 7*60 + 50
)

// PlanRow 是一行排期任务：账号 A 在业务日 D 为学校任务 T 预留的尝试位。
type PlanRow struct {
	AccountID    string
	SchoolTaskID string
	Day          string // YYYY-MM-DD，业务日
	RunAt        time.Time
}

// WindowRecord 是一条已发现的窗口：任务 T 在业务日 D 的起止与来源。
// 来源词表见 contract.WindowSource（platform/cache/fallback/override）。
type WindowRecord struct {
	SchoolTaskID string
	Day          string
	Start        time.Time
	End          time.Time
	Source       string
	Version      int
}

// WindowOverride 是一次人工窗口覆盖：任务 T 在业务日 D 改用指定起止。
// Reason 必填并留存，供事后复核；CreatedBy 是操作用户，
// 可为空：创建者被保留期清除后只留空引用。
type WindowOverride struct {
	SchoolTaskID string
	Day          string
	Start        time.Time
	End          time.Time
	Reason       string
	CreatedBy    string
}

// TaskInfo 是 school_tasks 的最小查询形状。
type TaskInfo struct {
	ID       string
	SchoolID string
	Name     string
	Enabled  bool
}

// AccountRef 是铺点所需的账号最小形状：ID 加其所有者。
// 所有者用于把用户自选偏移落到账号上。
type AccountRef struct {
	ID          string
	OwnerUserID string
}

// TimePref 是一条用户自选：OffsetMin 为 nil 表示无偏好。
// OffsetMin 是相对有效窗口起点的分钟数，按 5 分钟粒度存储。
type TimePref struct {
	UserID    string
	OffsetMin *int
	UpdatedAt time.Time
}

// WindowView 是管理面列表的一行：任务元数据加当日窗口缓存。
// Cached=false 表示当日窗口尚未发现，Start/End/Source 均无意义。
type WindowView struct {
	SchoolID   string
	SchoolName string
	TaskID     string
	TaskName   string
	Enabled    bool
	Day        string
	Start      time.Time
	End        time.Time
	Source     string
	Version    int
	Cached     bool
}

// TaskRow 是一行可执行的计划任务（worker 的输入形状）。
// State 取 core/status 的五态；Attempts 是已消耗的尝试数。
// Result 是行上最近一次写入的结论词或等待词（wait_*）：worker 靠它
// 读取等待型重扫的计数。终态行的词是业务结论，pending 行可能是等待词。
// OwnerUserID 是账号属主：worker 靠它从属主的图片库选图（P5-C）。
type TaskRow struct {
	ID           string
	AccountID    string
	SchoolID     string
	SchoolTaskID string
	Day          string
	RunAt        time.Time
	State        string
	Attempts     int
	// ClaimSeq 是本次认领的栅栏序号。结算与重排必须原样带回。
	ClaimSeq    int
	OwnerUserID string
	Result      string
}

// DayOutcomeCounts 是一个业务日的任务行汇总。
//
// 口径：
//   - 按 sign_tasks 任务行计数，不做账号去重（同账号多任务即多行）；
//   - Pending 含 pending 与 running：两者都还没有当日结论；
//   - FailedByOutcome 只含 state=failed 行的 result 原值；空串保留，
//     由汇总方归入 unknown。
//
// 定义在 core 而不是 app：adapters/pg 是端口的实现方，
// 不得反向依赖 app。
type DayOutcomeCounts struct {
	Total, Success, Failed, Skipped, Pending int
	FailedByOutcome                          map[string]int
}
