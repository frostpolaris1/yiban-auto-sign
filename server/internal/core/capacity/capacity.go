// Package capacity 是容量公式的唯一归属。
//
// # 功能
// 定义一个出口速率瓶颈下的窗口容量与缺口数。
// 本包**不**读配置、**不**排调度、**不**发网络请求。
//
// # 归属
// core 层纯领域包。入口、预览、worker 都向本包取容量结论，
// 不得在别处复写同一公式。
//
// # 复用
// Capacity、Shortfall。
//
// # 口径
// 容量 = floor(窗口秒 × 速率瓶颈 × util)，速率瓶颈 =
// min(通道吞吐 channels/avg，出口速率 rate)。新线单进程：
// 没有执行体数 K 这一维。窗口容不下一次尝试时容量为 0。
// util 必须落在 [0,1]，越界值被夹取。
package capacity

import "time"

// UtilDefault 是容量利用系数缺省：为重试与尾延迟降额。
const UtilDefault = 0.8

// Capacity 按窗口、通道数、单账号耗时、出口速率与利用系数算容量。
// 结果单位是**账号尝试数**（单账号一次完整签到链），不是 HTTP 请求数。
func Capacity(window time.Duration, channels int, avg time.Duration, rate, util float64) int {
	if window <= 0 || avg <= 0 {
		return 0
	}
	if channels < 0 {
		channels = 0
	}
	if util < 0 {
		util = 0
	}
	if util > 1 {
		util = 1
	}
	if rate <= 0 {
		rate = 0
	}
	throughput := float64(channels) / avg.Seconds() // 通道吞吐：尝试/s
	effective := throughput
	if rate < effective {
		effective = rate // 出口速率是硬上界：取两者较小者
	}
	if effective <= 0 {
		return 0
	}
	return int(float64(window.Seconds()) * effective * util)
}

// Shortfall 返回需求减容量的缺口；无缺口归零。
func Shortfall(need, have int) int {
	if need <= have {
		return 0
	}
	return need - have
}
