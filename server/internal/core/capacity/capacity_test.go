package capacity

import (
	"testing"
	"time"
)

// 新线单进程：容量公式没有执行体数 K 这一维。
// 出口速率是瓶颈：通道吞吐与出口速率取较小者。
func TestCapacity(t *testing.T) {
	cases := []struct {
		name     string
		window   time.Duration
		channels int
		avg      time.Duration
		rate     float64
		util     float64
		want     int
	}{
		{"缺省口径", 80 * time.Minute, 6, 3 * time.Second, 1.0, UtilDefault, 3840},
		{"通道是瓶颈", 10 * time.Minute, 2, 3 * time.Second, 10.0, 0.8, 320},
		{"速率是瓶颈", 10 * time.Minute, 8, 3 * time.Second, 0.5, 0.8, 240},
		{"小窗口仍有吞吐", 2 * time.Second, 6, 3 * time.Second, 1.0, 0.8, 1},
		{"零窗口", 0, 6, 3 * time.Second, 1.0, 0.8, 0},
	}
	for _, c := range cases {
		got := Capacity(c.window, c.channels, c.avg, c.rate, c.util)
		if got != c.want {
			t.Fatalf("%s: Capacity = %d, want %d", c.name, got, c.want)
		}
	}
}

func TestCapacityUtilClamp(t *testing.T) {
	// util 必须落在 [0,1]：大于 1 的输入被夹到 1，不产生虚高容量。
	if got := Capacity(time.Minute, 6, time.Second, 1.0, 2.0); got != 60 {
		t.Fatalf("util=2 应夹到 1（60 容量）, got %d", got)
	}
	if got := Capacity(time.Minute, 6, time.Second, 1.0, 0); got != 0 {
		t.Fatalf("util=0 应零容量, got %d", got)
	}
}

// 建议按需求判定要抬的一侧：容量够不给；加通道能装下才给数字；
// 加了还装不下改报出口；超过并发上限只报 workers 不给数。
func TestAdvise(t *testing.T) {
	cases := []struct {
		name     string
		window   time.Duration
		channels int
		avg      time.Duration
		rate     float64
		util     float64
		demand   int
		want     Advice
	}{
		// 5400 秒 × 1/3 × 0.7 = 1260，恰好等于需求：不给建议。
		{"容量恰好够", 90 * time.Minute, 1, 3 * time.Second, 1.0, 0.7, 1260,
			Advice{Binding: BindingWorkers}},
		// 400×3 ÷ (600×0.8) = 2.5 → 3 个通道；3 个通道容量 480 装得下。
		{"加通道可装下", 10 * time.Minute, 2, 3 * time.Second, 10.0, 0.8, 400,
			Advice{Binding: BindingWorkers, SuggestedWorkers: 3}},
		// 1000×300 ÷ (4800×0.8) = 78.125 → 79 > 64：并发上限内无法给数。
		{"超过并发上限", 80 * time.Minute, 1, 300 * time.Second, 1.0, 0.8, 1000,
			Advice{Binding: BindingWorkers}},
		// 需求 1500，当前容量 576；want 11，但 11 通道实得 1440，仍缺 60。
		{"加通道仍被出口封顶", 30 * time.Minute, 4, 10 * time.Second, 1.0, 0.8, 1500,
			Advice{Binding: BindingEgressRate}},
		{"零速率归出口", 10 * time.Minute, 2, 3 * time.Second, 0, 0.8, 1,
			Advice{Binding: BindingEgressRate}},
		{"需求装得下不给数", 80 * time.Minute, 6, 3 * time.Second, 10.0, 0.8, 10,
			Advice{Binding: BindingWorkers}},
	}
	for _, c := range cases {
		got := Advise(c.window, c.channels, c.avg, c.rate, c.util, c.demand)
		if got != c.want {
			t.Fatalf("%s: Advise = %+v, want %+v", c.name, got, c.want)
		}
	}
}

func TestShortfall(t *testing.T) {
	// 缺口 = 需求 − 容量，负值归零（无缺口）；恰等也归零（<= 边界，突变验证点）。
	if got := Shortfall(100, 80); got != 20 {
		t.Fatalf("Shortfall(100,80) = %d, want 20", got)
	}
	if got := Shortfall(80, 100); got != 0 {
		t.Fatalf("无缺口应归零, got %d", got)
	}
	if got := Shortfall(80, 80); got != 0 {
		t.Fatalf("恰好相等应归零, got %d", got)
	}
}

// 瓶颈判定与容量同口径：通道吞吐不大于出口速率归 workers，否则归 egress_rate。
func TestBindingOf(t *testing.T) {
	cases := []struct {
		name     string
		window   time.Duration
		channels int
		avg      time.Duration
		rate     float64
		util     float64
		want     Binding
	}{
		{"通道是瓶颈", 10 * time.Minute, 2, 3 * time.Second, 10.0, UtilDefault, BindingWorkers},
		{"速率是瓶颈", 10 * time.Minute, 8, 3 * time.Second, 0.5, UtilDefault, BindingEgressRate},
		{"恰好相等归通道", 10 * time.Minute, 2, 2 * time.Second, 1.0, UtilDefault, BindingWorkers},
		{"零速率归出口", 10 * time.Minute, 2, 3 * time.Second, 0, UtilDefault, BindingEgressRate},
		{"负速率归出口", 10 * time.Minute, 2, 3 * time.Second, -1, UtilDefault, BindingEgressRate},
		{"零窗口归通道", 0, 2, 3 * time.Second, 1.0, UtilDefault, BindingWorkers},
		{"零平均耗时归通道", 10 * time.Minute, 2, 0, 1.0, UtilDefault, BindingWorkers},
		// rate 判据优先：零窗口同时零速率时仍归出口。
		{"零窗口零速率归出口", 0, 2, 3 * time.Second, 0, UtilDefault, BindingEgressRate},
		{"零通道归通道", 10 * time.Minute, 0, 3 * time.Second, 1.0, UtilDefault, BindingWorkers},
		// util=0 时容量为 0，没有可用的吞吐比较：不能据此归咎出口。
		{"零利用系数归通道", 10 * time.Minute, 8, 3 * time.Second, 0.5, 0, BindingWorkers},
		// util 不参与瓶颈比较：越界值也不能翻转归属。
		{"util 越界不改变归属", 10 * time.Minute, 8, 3 * time.Second, 0.5, 2.0, BindingEgressRate},
	}
	for _, c := range cases {
		got := BindingOf(c.window, c.channels, c.avg, c.rate, c.util)
		if got != c.want {
			t.Fatalf("%s: BindingOf = %q, want %q", c.name, got, c.want)
		}
	}
}

// 建议值 = ceil(需求 × 平均耗时 ÷ (窗口 × util))；非正输入归 0。
func TestSuggestedWorkers(t *testing.T) {
	cases := []struct {
		name   string
		demand int
		window time.Duration
		avg    time.Duration
		util   float64
		want   int
	}{
		// 100×60 ÷ (4800×0.8) = 1.5625 → 2。
		{"向上取整", 100, 80 * time.Minute, 60 * time.Second, 0.8, 2},
		// 64×60 ÷ (4800×0.8) = 1，整除不多要一个。
		{"整除不多要", 64, 80 * time.Minute, 60 * time.Second, 0.8, 1},
		{"零需求", 0, 80 * time.Minute, 60 * time.Second, 0.8, 0},
		{"零窗口", 100, 0, 60 * time.Second, 0.8, 0},
		{"零平均耗时", 100, 80 * time.Minute, 0, 0.8, 0},
		{"零利用系数", 100, 80 * time.Minute, 60 * time.Second, 0, 0},
		// 100000×60 ÷ (4800×0.8) = 1562.5 → 1563，超过 64 由调用方决定不给建议。
		{"超过上限也如实返回", 100000, 80 * time.Minute, 60 * time.Second, 0.8, 1563},
		// util 与 Capacity 同口径夹到 1：100×60 ÷ 4800 = 1.25 → 2。
		{"util 越界夹取", 100, 80 * time.Minute, 60 * time.Second, 2.0, 2},
	}
	for _, c := range cases {
		got := SuggestedWorkers(c.demand, c.window, c.avg, c.util)
		if got != c.want {
			t.Fatalf("%s: SuggestedWorkers = %d, want %d", c.name, got, c.want)
		}
	}
	if MaxWorkers != 64 {
		t.Fatalf("MaxWorkers = %d, want 64（与配置上限一致）", MaxWorkers)
	}
}
