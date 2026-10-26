package plan

import "time"

// 时段几何：片、顺延、确定性相位与铺点。
//
// 这组纯函数是 P4 计划层的几何判据：
// 锚点取整（NextSlot）、片内就近顺延（NearestFree，同距离取更早）、
// 槽内确定性相位（PhaseOf）与整批铺点（Spread，front 模式收进前段，
// 装不下自然退化 uniform）。全部函数无全局状态、可重放：
// 同输入必同输出，这是"计划可审计"的底座。

// Window 是排期用的几何窗口：起点为 0 号片，终点是硬上界。
// 它是**相对偏移**窗口（距窗口起点的时长），不是绝对时刻；
// 绝对时刻的换算属 app 层。
type Window struct {
	Start time.Duration // 窗口起点（恒为 0 号片的基准）
	End   time.Duration // 窗口终点（含起点不含终点）
}

// NextSlot 返回 offset 所在的片号（与 SlotOf 同为 floor 语义）。
// 片号以窗口起点为 0；片起点越出窗口返回 false。
func NextSlot(w Window, offset time.Duration) (int, bool) {
	if offset < w.Start {
		offset = w.Start
	}
	k := SlotOf(offset)
	if span(k) >= w.End {
		return 0, false
	}
	return k, true
}

// span 返回片号 k 的起点偏移。
func span(k int) time.Duration {
	return time.Duration(k) * SlotSpan
}

// NearestFree 在偏好片 pref 附近找未满片：同距离优先更早的片。
// countOf 查询片内已占数，cap 是片容量；搜索半径 radius 片（向两侧）。
// 片号负值不参与判定：counts 对缺失键返回 0（空片），负号片不是真实片，
// 误判成空会提前返回 -1 哨兵、漏看另一侧的真实空片。
// 全满返回 -1。
func NearestFree(counts map[int]int, pref, cap, radius int, countOf func(int) int) int {
	get := countOf
	if counts != nil {
		get = func(k int) int { return counts[k] }
	}
	for d := 0; d <= radius; d++ {
		for _, k := range []int{pref - d, pref + d} {
			if k < 0 {
				continue
			}
			if get(k) < cap {
				return k
			}
		}
	}
	return -1
}

// PhaseOf 返回 key 在 slots 个槽内的确定性槽位：sha256 前 8 字节取高 53 位
// 按比例映射（实现见 hash.go）。同一 key 恒得同一槽，跨天由调用方把 day 拼进 key 重排。
func PhaseOf(key string, slots int) int {
	if slots < 1 {
		slots = 1
	}
	return int(hash01(key) * float64(slots))
}

// Spread 把 n 个账号顺序铺进每片 slots 个槽的片里，返回各行的偏移（按行序）。
// 行 i 落在片 i/slots、槽 i%slots——每片装满 slots 人再进下一片。
// 「front 收进前段」由调用方选择：把行偏移过 NextSlot 得片号，与 front 片数比较；
// 本函数只负责"装满一片再进下一片"的几何，不做模式分支。
// 偏移确定性可重放。
func Spread(n, slots int) []time.Duration {
	if n < 0 {
		n = 0
	}
	if slots < 1 {
		slots = 1
	}
	offs := make([]time.Duration, n)
	for i := 0; i < n; i++ {
		k := i / slots
		slot := i % slots
		offs[i] = span(k) + time.Duration(slot)*SlotSpan/time.Duration(slots)
	}
	return offs
}
