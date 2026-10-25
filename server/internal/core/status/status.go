// Package status is the only definition of task states.
package status

// TaskState is the persisted lifecycle state of one sign attempt.
type TaskState string

const (
	TaskPending TaskState = "pending"
	TaskRunning TaskState = "running"
	TaskSuccess TaskState = "success"
	TaskFailed  TaskState = "failed"
	TaskSkipped TaskState = "skipped"
)

// Valid reports whether s is a known state.
func (s TaskState) Valid() bool {
	switch s {
	case TaskPending, TaskRunning, TaskSuccess, TaskFailed, TaskSkipped:
		return true
	default:
		return false
	}
}

// Terminal reports whether no further transition is expected.
func (s TaskState) Terminal() bool {
	switch s {
	case TaskSuccess, TaskFailed, TaskSkipped:
		return true
	default:
		return false
	}
}
