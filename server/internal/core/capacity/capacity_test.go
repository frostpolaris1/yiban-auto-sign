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
