package plan

import (
	"testing"
	"time"
)

// SpreadWithin：front 顺序装满前段；uniform 全窗分层铺开。
// 两种模式都不得越出 nSlices 片范围——这是"计划不越窗"的几何前提。
func TestSpreadWithinFrontPacksEarliest(t *testing.T) {
	offs := SpreadWithin(60, 10, 16, true)
	for i, off := range offs {
		if off >= 6*5*time.Minute {
			// front 装 60 人只需 6 片（10 槽/片）；第 60 人恰在 6 片边界内。
			if i < 60 {
				t.Fatalf("front 第 %d 行越出前 6 片: %v", i, off)
			}
		}
		if off < 0 || off >= 16*5*time.Minute {
			t.Fatalf("front 第 %d 行越窗: %v", i, off)
		}
	}
}

func TestSpreadWithinUniformStratifies(t *testing.T) {
	// uniform：60 人铺 16 片，每片至多 ceil(60/16)=4 人；首片与末片都有人。
	offs := SpreadWithin(60, 10, 16, false)
	per := map[int]int{}
	for _, off := range offs {
		k := SlotOf(off)
		per[k]++
		if off < 0 || off >= 16*5*time.Minute {
			t.Fatalf("uniform 越窗: %v", off)
		}
	}
	if per[0] == 0 || per[15] == 0 {
		t.Fatalf("uniform 未铺满首末片: %v", per)
	}
	if len(per) < 16 {
		t.Fatalf("uniform 只用了 %d 片, want 16", len(per))
	}
}

func TestSpreadWithinDeterministic(t *testing.T) {
	a := SpreadWithin(30, 4, 8, false)
	b := SpreadWithin(30, 4, 8, false)
	for i := range a {
		if a[i] != b[i] {
			t.Fatalf("不可重放: 行 %d", i)
		}
	}
}

func TestSpreadWithinDegenerate(t *testing.T) {
	if got := SpreadWithin(0, 4, 8, true); len(got) != 0 {
		t.Fatalf("n=0 应有 0 行, got %d", len(got))
	}
	// slots/nSlices 非法时按 1 处理，且全部落在片内。
	for _, off := range SpreadWithin(3, 0, 0, false) {
		if off < 0 || off >= 5*time.Minute {
			t.Fatalf("退化参数越界: %v", off)
		}
	}
}
