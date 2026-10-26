// Package diagnostic 定义诊断读模型的领域取值与建议词表（P6）。
//
// # 功能
// 任务行、窗口、出口标签与状态计数四个只读形状；
// 用户建议动作的封闭词表（wait|retry_now|re_arm|contact_admin）
// 与其映射函数。本包**不**查库、**不**实现 HTTP。
//
// # 归属
// core 层纯领域包。app/diagnostic 声明端口，adapters/pg 实现；
// 端口与本包共享同一组类型，适配器因此不必反向依赖 app 层。
//
// # 复用
// TaskRow、WindowInfo、EgressInfo、View；Suggestion 词表与
// SuggestionFor；StaleAfter。
//
// # 口径
// stale = now - TaskRow.UpdatedAt > StaleAfter。
// SuggestionFor 的窗口未知分支按保守方向处理：不给 retry_now。
package diagnostic

import (
	"time"

	coremask "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
)

// Suggestion 是给用户的封闭建议词表。
const (
	SuggestWait         = "wait"          // 系统处理中或当日已了结
	SuggestRetryNow     = "retry_now"     // 失败且窗口仍开：可手动再签
	SuggestReArm        = "re_arm"        // 失败且窗口已关或未知：当日无可为，等重排
	SuggestContactAdmin = "contact_admin" // 凭据不可读：只有管理员能修
)

// StaleAfter 是轮询的陈旧阈值：行 updated_at 距今超过它即 stale。
const StaleAfter = 90 * time.Second

// TaskRow 是当日任务行的只读形状（实现见 adapters/pg）。
type TaskRow struct {
	TaskID       string
	SchoolTaskID string
	AccountID    string
	State        string
	Result       string
	Attempts     int
	RunAt        time.Time
	UpdatedAt    time.Time
}

// WindowInfo 是当日窗口缓存行。
type WindowInfo struct {
	Start   time.Time
	End     time.Time
	Source  string
	Version int
}

// EgressInfo 是账号粘性出口的标签。
type EgressInfo struct {
	Identity string
}

// View 是 SSE snapshot 帧的数据：业务日与状态计数。
type View struct {
	Day    string         `json:"day"`
	States map[string]int `json:"states"`
}

// SuggestionFor 按状态与结论词映射封闭建议词。
//
//   - failed + credential_invalid → contact_admin：只有管理员能修凭据；
//   - failed + 窗口仍开 → retry_now：手动再签是正当动作；
//   - failed + 窗口已关或未知 → re_arm：当日无可为，等系统重排；
//   - 其余状态（含未知状态）→ wait。
//
// result 先经掩码：结论词本无敏感内容，此步是纵深防御。
func SuggestionFor(state, result string, win *WindowInfo, now time.Time) string {
	result = coremask.MaskText(result)
	if corestatus.TaskState(state) != corestatus.TaskFailed {
		return SuggestWait
	}
	if result == "credential_invalid" {
		return SuggestContactAdmin
	}
	if win != nil && !now.Before(win.Start) && now.Before(win.End) {
		return SuggestRetryNow
	}
	return SuggestReArm
}
