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
// Capacity、Shortfall、BindingOf、Advise、SuggestedWorkers。
//
// # 口径
// 容量 = floor(窗口秒 × 速率瓶颈 × util)，速率瓶颈 =
// min(通道吞吐 channels/avg，出口速率 rate)。新线单进程：
// 没有执行体数 K 这一维。窗口容不下一次尝试时容量为 0。
// util 必须落在 [0,1]，越界值被夹取。
// BindingOf 报当前瓶颈；Advise 按需求给出要抬的一侧与建议通道数：
// 加通道（不超过 MaxWorkers）能装下需求才归 workers 并给数；
// 加了通道仍被出口速率封顶则归 egress_rate 且不给数。
// 入口与 UI 只消费本包结论，不复写公式。
package capacity

import (
	"math"
	"time"
)

// UtilDefault 是容量利用系数缺省：为重试与尾延迟降额。
const UtilDefault = 0.8

// MaxWorkers 是并发通道的建议上限，与配置 YIBAN_WORKERS 的上限一致。
const MaxWorkers = 64

// Binding 是容量瓶颈的归属。
type Binding string

const (
	// BindingWorkers 表示通道吞吐是瓶颈：加并发通道能提升容量。
	BindingWorkers Binding = "workers"
	// BindingEgressRate 表示出口速率是瓶颈：加并发通道不提升容量。
	BindingEgressRate Binding = "egress_rate"
)

// clampUtil 把利用系数夹进 [0,1]；越界值不产生虚高或负容量。
func clampUtil(util float64) float64 {
	if util < 0 {
		return 0
	}
	if util > 1 {
		return 1
	}
	return util
}

// Capacity 按窗口、通道数、单账号耗时、出口速率与利用系数算容量。
// 结果单位是**账号尝试数**（单账号一次完整签到链），不是 HTTP 请求数。
func Capacity(window time.Duration, channels int, avg time.Duration, rate, util float64) int {
	if window <= 0 || avg <= 0 {
		return 0
	}
	if channels < 0 {
		channels = 0
	}
	util = clampUtil(util)
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

// BindingOf 判定当前容量瓶颈归属，与 Capacity 同口径。
//
// rate 非正时容量由出口速率卡死，归 egress_rate；该判据优先于窗口与耗时。
// 通道吞吐不大于出口速率时归 workers，否则归 egress_rate。
// 窗口、平均耗时或利用系数非正时容量为 0，没有可用的吞吐比较，归 workers：
// 它不是出口瓶颈的证据，调用方不应据此建议提高出口速率。
// 本函数只报当前瓶颈；Advise 会按需求把归属升级为「要抬的那一侧」。
func BindingOf(window time.Duration, channels int, avg time.Duration, rate, util float64) Binding {
	if rate <= 0 {
		return BindingEgressRate
	}
	if window <= 0 || avg <= 0 {
		return BindingWorkers
	}
	if clampUtil(util) == 0 {
		return BindingWorkers
	}
	if channels < 0 {
		channels = 0
	}
	if float64(channels)/avg.Seconds() <= rate {
		return BindingWorkers
	}
	return BindingEgressRate
}

// SuggestedWorkers 返回满足需求的并发通道数：ceil(需求 × 平均耗时 ÷ (窗口 × util))。
//
// 结果为满足需求所需的总通道数，不是增量。非正输入或利用系数为 0 归 0；
// util 与 Capacity 同口径夹到 [0,1]。是否值得建议（大于当前值、不超过
// MaxWorkers 且真的装得下需求）由 Advise 判定。
func SuggestedWorkers(demand int, window, avg time.Duration, util float64) int {
	if demand <= 0 || window <= 0 || avg <= 0 {
		return 0
	}
	util = clampUtil(util)
	if util == 0 {
		return 0
	}
	need := float64(demand) * avg.Seconds() / (window.Seconds() * util)
	want := int(math.Ceil(need))
	if want < 1 {
		return 1
	}
	return want
}

// Advice 是容量建议：Binding 是满足需求要抬的一侧，
// SuggestedWorkers 为 0 表示不给数字建议。
type Advice struct {
	Binding          Binding
	SuggestedWorkers int
}

// Advise 按当前配置与需求给出要抬的一侧和建议通道数。
//
// 当前容量已够时建议为 0，Binding 仍报当前瓶颈。
// 加通道（want 大于当前且不超过 MaxWorkers）能装下需求时归 workers 并给数。
// want 不超过 MaxWorkers 但加了通道仍装不下时，出口速率是硬上界：
// 归 egress_rate 且不给数字。
// want 超过 MaxWorkers 时并发已到上限、无法给数：归 workers。
// 非法输入下 SuggestedWorkers 归 0，此时保留当前瓶颈且不给建议。
func Advise(window time.Duration, channels int, avg time.Duration, rate, util float64, demand int) Advice {
	binding := BindingOf(window, channels, avg, rate, util)
	if Capacity(window, channels, avg, rate, util) >= demand {
		return Advice{Binding: binding}
	}
	want := SuggestedWorkers(demand, window, avg, util)
	if want < 1 {
		return Advice{Binding: binding}
	}
	if want > MaxWorkers {
		return Advice{Binding: BindingWorkers}
	}
	if Capacity(window, want, avg, rate, util) < demand {
		return Advice{Binding: BindingEgressRate}
	}
	if want <= channels {
		return Advice{Binding: binding}
	}
	return Advice{Binding: BindingWorkers, SuggestedWorkers: want}
}

// Shortfall 返回需求减容量的缺口；无缺口归零。
func Shortfall(need, have int) int {
	if need <= have {
		return 0
	}
	return need - have
}
