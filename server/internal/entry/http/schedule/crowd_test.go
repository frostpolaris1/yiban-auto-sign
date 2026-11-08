package schedule

import "testing"

// crowdPct 的旧线档位：10% 一档，满员 100，未满封顶 90，有人至少 10。
func TestCrowdPctBands(t *testing.T) {
	for _, tc := range []struct {
		count, capacity, want int
	}{
		{0, 20, 0},
		{1, 20, 10},
		{3, 20, 20},
		{10, 20, 50},
		{18, 20, 90},
		{19, 20, 90}, // 95% 不得四舍五入成满员
		{20, 20, 100},
		{21, 20, 100},
		{1, 4, 20}, // 25%，银行家舍入
		{1, 1, 100},
		{0, 1, 0},
	} {
		if got := crowdPct(tc.count, tc.capacity); got != tc.want {
			t.Errorf("crowdPct(%d,%d) = %d, want %d", tc.count, tc.capacity, got, tc.want)
		}
	}
}
