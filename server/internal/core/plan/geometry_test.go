package plan

import (
	"testing"
	"time"
)

// 锚点取整：给一个偏移，返回它所在的 5 分钟片号（floor，与 SlotOf 同语义）。
// 片号以窗口起点为 0，片起点越出窗口返回 false。
func TestNextSlot(t *testing.T) {
	window := Window{Start: 0, End: 80 * time.Minute}
	cases := []struct {
		offset time.Duration
		want   int
	}{
		{0, 0},
		{time.Second, 0},
		{4*time.Minute + 59*time.Second, 0},
		{5 * time.Minute, 1},
		{12 * time.Minute, 2},
	}
	for _, c := range cases {
		got, ok := NextSlot(window, c.offset)
		if !ok || got != c.want {
			t.Fatalf("NextSlot(%v) = (%d,%v), want (%d,true)", c.offset, got, ok, c.want)
		}
	}
	if _, ok := NextSlot(Window{Start: 0, End: 5 * time.Minute}, 5*time.Minute); ok {
		t.Fatal("片号越出窗口应返回 false")
	}
}

// 就近顺延：偏好片满员时，在同距离的更早片与更晚片之间取更早的。
func TestNearestFreePrefersEarlier(t *testing.T) {
	// 15 满员，两侧各差一（cap=2）。
	got := NearestFree(map[int]int{14: 1, 15: 2, 16: 1}, 15, 2, 5, nil)
	if got != 14 {
		t.Fatalf("NearestFree = %d, want 14（同距离优先更早）", got)
	}
}

// 边界：偏好片是 0 号且满员时，负片号不算空片，必须看得到 1 号。
// 缺此判据时 d=1 先探 -1，counts 缺键返回 0 被当空片——哨兵 -1 提前返回。
func TestNearestFreeNegativeSkipped(t *testing.T) {
	got := NearestFree(map[int]int{0: 2}, 0, 2, 5, nil)
	if got != 1 {
		t.Fatalf("NearestFree = %d, want 1（负片号不入判据）", got)
	}
}

func TestNearestFreeLaterSide(t *testing.T) {
	got := NearestFree(nil, 15, 2, 5, func(k int) int {
		if k == 16 {
			return 0
		}
		return 2
	})
	if got != 16 {
		t.Fatalf("NearestFree = %d, want 16（只有更晚有空）", got)
	}
}

func TestNearestFreeNone(t *testing.T) {
	got := NearestFree(nil, 15, 2, 2, func(int) int { return 2 })
	if got != -1 {
		t.Fatalf("全满应返回 -1, got %d", got)
	}
}

// 溢出半径有界：从偏好片向两侧逐格搜索，最多搜 radius 格（每侧 radius 个片号）。
func TestNearestFreeRadius(t *testing.T) {
	seen := map[int]bool{}
	got := NearestFree(nil, 10, 2, 3, func(k int) int {
		seen[k] = true
		return 9
	})
	if got != -1 {
		t.Fatalf("应全满, got %d", got)
	}
	for k := range seen {
		if k < 10-3 || k > 10+3 {
			t.Fatalf("搜索越界: %d", k)
		}
	}
	if !seen[10-3] || !seen[10+3] {
		t.Fatal("半径边界片没被探到")
	}
}

// 确定性相位：同输入同输出，且相位落在 [0, slot) 内。
func TestPhaseOf(t *testing.T) {
	slots := 12
	seen := map[float64]bool{}
	for i := 0; i < 50; i++ {
		key := string(rune('a'+i%26)) + string(rune('0'+i/26))
		p := PhaseOf(key, slots)
		if p < 0 || p >= slots {
			t.Fatalf("PhaseOf(%q) = %d 越界", key, p)
		}
		seen[float64(p)] = true
	}
	// 50 个不同键全落同一槽是哈希失效；任何键重查必须同值。
	if len(seen) < 2 {
		t.Fatal("相位零分布，判据失真")
	}
	for i := 0; i < 26; i++ {
		key := string(rune('a' + i))
		if PhaseOf(key, slots) != PhaseOf(key, slots) {
			t.Fatalf("PhaseOf(%q) 不可重放", key)
		}
	}
}

// Spread 铺点：N 个账号顺序填充，同输入同输出；首行从 0 起。
func TestSpreadDeterministic(t *testing.T) {
	a := Spread(30, 10)
	b := Spread(30, 10)
	if len(a) != 30 {
		t.Fatalf("Spread 行数 = %d, want 30", len(a))
	}
	for i := range a {
		if a[i] != b[i] {
			t.Fatalf("Spread 不可重放: 行 %d 偏移不同", i)
		}
	}
	if a[0] != 0 {
		t.Fatalf("首行应从 0 偏移起, got %v", a[0])
	}
}

func TestSpreadFront(t *testing.T) {
	// front：调用方取前 60 行即前 6 片（每片 10 槽），顺序填充无人越界（片宽 5 分钟）。
	got := Spread(60, 10)
	for i, off := range got {
		if off >= 6*5*time.Minute {
			t.Fatalf("front 模式行 %d 偏移越界: %v", i, off)
		}
	}
}

func TestSpreadOverflowFallsBackUniform(t *testing.T) {
	// 装不下时行溢出到前段之外：80 人按 10 槽/片铺满 8 片，第 7、8 片各 10 人。
	got := Spread(80, 10)
	count := 0
	for _, off := range got {
		if off >= 6*5*time.Minute {
			count++
		}
	}
	if count != 20 {
		t.Fatalf("溢出行数 = %d, want 20", count)
	}
}
