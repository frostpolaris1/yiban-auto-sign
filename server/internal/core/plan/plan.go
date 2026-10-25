// Package plan 是时段几何的唯一定义。
//
// # 功能
// 定义用户可选时段宽度与偏移到槽位的换算。
// 本包**不**排期、**不**读配置、**不**发请求、**不**存数据。
//
// # 归属
// core 层纯领域包。用例层按槽位组织时段，不得另写换算。
//
// # 复用
// SlotSpan、SlotOf。
//
// # 口径
// 槽宽固定 5 分钟。偏移以窗口起点为 0。
// 负偏移夹到 0 号槽，函数对全部输入有定义。
package plan

import "time"

// SlotSpan 是用户可选的时段宽度，5 分钟。
const SlotSpan = 5 * time.Minute

// SlotOf 把距窗口起点的偏移映射为槽位序号。
// 负偏移夹到 0 号槽。
func SlotOf(offset time.Duration) int {
	if offset < 0 {
		return 0
	}
	return int(offset / SlotSpan)
}
