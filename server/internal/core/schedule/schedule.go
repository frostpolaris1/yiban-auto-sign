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
// PlanRow、WindowRecord、State 转换。
//
// # 口径
// 一行计划 = 一个账号 × 一个学校任务 × 一个业务日。
// 状态词表与 core/status 的 TaskState 同形（pending/running/
// success/failed/skipped），转换只在此一处。
package schedule

import "time"

// PlanRow 是一行排期任务：账号 A 在业务日 D 为学校任务 T 预留的尝试位。
type PlanRow struct {
	AccountID    string
	SchoolTaskID string
	Day          string // YYYY-MM-DD，业务日
	RunAt        time.Time
}

// WindowRecord 是一条已发现的窗口：任务 T 在业务日 D 的起止与来源。
// 来源词表见 contract.WindowSource（platform/cache/fallback）。
type WindowRecord struct {
	SchoolTaskID string
	Day          string
	Start        time.Time
	End          time.Time
	Source       string
	Version      int
}
