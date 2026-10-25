// Package clock 是墙钟与业务时间的唯一来源。
//
// # 功能
// 提供 Clock 接口、System 与 Fixed 两种实现，以及业务日与时间戳换算。
// 本包**不**做排期、**不**读配置、**不**存时间。
//
// # 归属
// core 层纯领域包。**全仓只有本包可调用 time.Now 或加载时区**，
// 门禁见 canonical.tsv 的 clock 规则。测试注入 Fixed。
//
// # 复用
// Clock、System、Fixed；BusinessDay、FromUnix、LoadLocation。
//
// # 口径
// 业务日按院校时区的 YYYY-MM-DD 计算；loc 为 nil 时按 UTC。
// 平台 epoch 秒一律经 FromUnix 换算，别处不做时区解释。
package clock

import "time"

// Clock 读取当前时刻。生产用 System，测试用 Fixed。
type Clock interface {
	Now() time.Time
}

// System 读取真实墙钟。
type System struct{}

// Now 返回当前时刻。
func (System) Now() time.Time { return time.Now() }

// Fixed 返回固定时刻。**测试只能用这个时钟**。
type Fixed struct {
	T time.Time
}

// Now 返回固定时刻。
func (f Fixed) Now() time.Time { return f.T }

// BusinessDay 按 loc 格式化为业务日 YYYY-MM-DD。
// loc 为 nil 时按 UTC，函数因此对全部输入有定义。
func BusinessDay(now time.Time, loc *time.Location) string {
	if loc == nil {
		loc = time.UTC
	}
	return now.In(loc).Format("2006-01-02")
}

// FromUnix 把平台 epoch 秒转换为 loc 时区下的时刻。
// loc 为 nil 时按 UTC。
func FromUnix(sec int64, loc *time.Location) time.Time {
	if loc == nil {
		loc = time.UTC
	}
	return time.Unix(sec, 0).In(loc)
}

// LoadLocation 是加载 IANA 时区名的唯一入口。
func LoadLocation(name string) (*time.Location, error) {
	return time.LoadLocation(name)
}
