package window

import (
	"testing"
	"time"
)

// 固定测试窗口：2026-10-29 06:30~07:50 北京时间。
var (
	loc    = time.FixedZone("CST", 8*3600)
	start  = time.Date(2026, 10, 29, 6, 30, 0, 0, loc)
	end    = time.Date(2026, 10, 29, 7, 50, 0, 0, loc)
	window = Window{Start: start, End: end}
)

func at(h, m, s int) time.Time {
	return time.Date(2026, 10, 29, h, m, s, 0, loc)
}

func TestValidate(t *testing.T) {
	if err := Validate(window); err != nil {
		t.Fatalf("有效窗口报错: %v", err)
	}
	if err := Validate(Window{Start: end, End: start}); err == nil {
		t.Fatal("起 >= 止的窗口未报错")
	}
	if err := Validate(Window{Start: start, End: start}); err == nil {
		t.Fatal("零宽窗口未报错")
	}
}

func TestOpenClosedThreeParts(t *testing.T) {
	// 三段谓词：未开 / 进行中 / 已关。边界都是闭区间。
	if window.Open(at(6, 29, 59)) {
		t.Fatal("06:29:59 不该已开")
	}
	if !window.Open(start) {
		t.Fatal("06:30:00 该已开（闭区间）")
	}
	if window.Closed(at(7, 49, 59)) {
		t.Fatal("07:49:59 不该已关")
	}
	if !window.Closed(end) {
		t.Fatal("07:50:00 该已关（>= 判定，闭区间）")
	}
	if !window.Open(at(7, 49, 0)) || window.Closed(at(7, 49, 0)) {
		t.Fatal("07:49 该处于进行中段（已开且未关）")
	}
}

func TestRemaining(t *testing.T) {
	if got := window.Remaining(at(6, 0, 0)); got != 110*time.Minute {
		t.Fatalf("Remaining(06:00) = %v, want 110m", got)
	}
	if got := window.Remaining(at(8, 0, 0)); got != -10*time.Minute {
		t.Fatalf("Remaining(08:00) = %v, want -10m（可为负）", got)
	}
}

func TestClamp(t *testing.T) {
	if got := window.Clamp(at(9, 0, 0)); !got.Equal(end) {
		t.Fatalf("Clamp(09:00) = %v, want 窗口终点", got)
	}
	if got := window.Clamp(at(7, 0, 0)); !got.Equal(at(7, 0, 0)) {
		t.Fatalf("窗内落点被改动: %v", got)
	}
}

func TestEffective(t *testing.T) {
	// 正常路径：两端各让 60s，逐值不变。
	got, clamped := window.Effective(60*time.Second, 60*time.Second)
	if clamped {
		t.Fatal("正常缓冲不该收缩")
	}
	if !got.Start.Equal(at(6, 31, 0)) || !got.End.Equal(at(7, 49, 0)) {
		t.Fatalf("Effective = [%v, %v], want [06:31, 07:49]", got.Start, got.End)
	}
}

func TestEffectiveShrinksOversizedEdges(t *testing.T) {
	// 缓冲之和**恰好等于**窗口宽度：这是 >= 与 > 的分界，突变验证要求必须踩到。
	// 5 分钟窗口、前后各 2.5 分钟（和恰 5 分钟）：budget = 300s×0.2 = 60s，scale = 0.2，
	// 前后各收成 30s。牺牲精修、绝不牺牲管理员（平台）意图。
	small := Window{Start: at(6, 30, 0), End: at(6, 35, 0)}
	got, clamped := small.Effective(150*time.Second, 150*time.Second)
	if !clamped {
		t.Fatal("吃满窗口的缓冲该报收缩")
	}
	if !got.Start.Equal(at(6, 30, 30)) || !got.End.Equal(at(6, 34, 30)) {
		t.Fatalf("收缩后 = [%v, %v], want [06:30:30, 06:34:30]", got.Start, got.End)
	}
}

func TestTwoSchoolsIndependent(t *testing.T) {
	// 两校不同窗口互不串窗：谓词只吃自己的窗口值，无全局状态可串。
	other := Window{Start: at(20, 0, 0), End: at(21, 0, 0)}
	now := at(7, 0, 0)
	if !window.Open(now) {
		t.Fatal("甲校 07:00 该在窗口内")
	}
	if other.Open(now) || other.Closed(now) {
		t.Fatal("乙校 07:00 该处于未开段，不受甲校影响")
	}
}
