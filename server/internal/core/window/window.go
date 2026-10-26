// Package window 是签到窗口几何的唯一定义。
//
// # 功能
// 定义窗口的三段谓词（未开／进行中／已关）、剩余秒、落点夹取，
// 以及有效窗口的掐头去尾缓冲与超界收缩。
// 本包**不**取窗口值、**不**读配置、**不**判平台状态位。
// 窗口值的唯一来源是平台 signPosition 的 Range（来源标记在
// internal/signing/contract 的 WindowSource）。
//
// # 归属
// core 层纯领域包。排计划、判关闭、算容量三处必须同源调本包。
//
// # 复用
// Window、Validate、Effective。
//
// # 口径
// 谓词是闭区间：恰在起点算已开；恰在终点算已关（>= 判定）。
// Remaining 可为负（窗口已过多久）。
// Effective 退化处置：缓冲之和**达到**窗口宽度时保窗口、等比收缩缓冲
// 到窗口宽度的 20%（牺牲精修、绝不牺牲管理员意图），
// 并以第二返回值报告收缩。
package window

import "time"

// EdgeBudgetRatio 是缓冲合计占窗口宽度的收缩阈值：合计**达到**该比例即收缩。
// 收缩后有效窗口恰为窗口宽度的 (1 − 本值)。
const EdgeBudgetRatio = 0.2

// Window 是一个签到窗口：绝对时刻，带时区语义由调用方保证。
// 平台 epoch 秒经 clock.FromUnix 换算后传入。
type Window struct {
	Start time.Time // 窗口开始（含）
	End   time.Time // 窗口结束（不含：>= End 判已关）
}

// Validate 拒绝起 >= 止的窗口：零宽与倒挂都不能参与排期。
func Validate(w Window) error {
	if !w.Start.Before(w.End) {
		return ErrInvalid
	}
	return nil
}

// Open 报告 now 是否已进入窗口（含起点）。
// 与 Closed 分开：只判"关没关"含不住"还没开"。
func (w Window) Open(now time.Time) bool {
	return !now.Before(w.Start)
}

// Closed 报告窗口是否已关（now >= End，闭区间口径）。
// 排计划与判关闭必须用本谓词，不得各写一份比较。
func (w Window) Closed(now time.Time) bool {
	return !now.Before(w.End)
}

// Remaining 返回从 now 到窗口终点的剩余秒；可为负。
func (w Window) Remaining(now time.Time) time.Duration {
	return w.End.Sub(now)
}

// Clamp 把落点夹进窗口：早于起点归起点，晚于终点归终点。
func (w Window) Clamp(t time.Time) time.Time {
	if t.Before(w.Start) {
		return w.Start
	}
	if t.After(w.End) {
		return w.End
	}
	return t
}

// Effective 返回掐头去尾后的有效窗口（front/back 各让一段）。
// 缓冲之和吃满窗口时保窗口、按比例收缩缓冲，第二返回值报收缩。
func (w Window) Effective(front, back time.Duration) (Window, bool) {
	width := w.End.Sub(w.Start)
	if width <= 0 {
		return Window{Start: w.Start, End: w.End}, true
	}
	if front+back >= width {
		// width > 0 已由上面的分支保证，front+back >= width > 0 蕴含 total > 0：
		// 除零不可达，无需再防。
		budget := time.Duration(float64(width) * EdgeBudgetRatio)
		total := front + back
		scale := float64(budget) / float64(total)
		front = time.Duration(float64(front) * scale)
		back = time.Duration(float64(back) * scale)
		return Window{Start: w.Start.Add(front), End: w.End.Add(-back)}, true
	}
	return Window{Start: w.Start.Add(front), End: w.End.Add(-back)}, false
}
