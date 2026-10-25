// Package status 是任务状态的唯一定义。
//
// # 功能
// 定义一次签到尝试的持久化生命周期状态，并给出终态判据。
// 本包**不**执行状态迁移、**不**写库、**不**判定签到成败。
//
// # 归属
// core 层纯领域包。它与账号状态（core/account）是两套词表。
//
// # 复用
// TaskState 及 Valid、Terminal；五个状态常量。
//
// # 口径
// 终态只有 success、failed、skipped。
// pending 与 running **不是终态**，必须继续推进。
package status

// TaskState 是一次签到尝试的持久化生命周期状态。
type TaskState string

const (
	TaskPending TaskState = "pending"
	TaskRunning TaskState = "running"
	TaskSuccess TaskState = "success"
	TaskFailed  TaskState = "failed"
	TaskSkipped TaskState = "skipped"
)

// Valid 报告 s 是否为已知状态。
func (s TaskState) Valid() bool {
	switch s {
	case TaskPending, TaskRunning, TaskSuccess, TaskFailed, TaskSkipped:
		return true
	default:
		return false
	}
}

// Terminal 报告是否不再有后续迁移。
func (s TaskState) Terminal() bool {
	switch s {
	case TaskSuccess, TaskFailed, TaskSkipped:
		return true
	default:
		return false
	}
}
