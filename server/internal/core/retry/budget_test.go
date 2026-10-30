package retry

import (
	"testing"
	"time"
)

var farFuture = time.Unix(1<<62, 0) // 实际无穷大的窗口终点

// 分级预算按 contract 的 Outcome 词表取值（AGENTS §26：状态语义自持，
// 词表以 internal/signing/contract 为准，本包不另造一套）。
func TestBudgetFor(t *testing.T) {
	cases := []struct {
		outcome string
		want    int
	}{
		{"transient", 3},
		{"pending_upstream", 3},
		{"unknown", 3},
		{"challenge", 1},
		{"blocked", 1},
		{"credential_invalid", 1},
		{"device_unbound", 1},
		{"invalid_target", 1},
		{"rejected", 1},
		{"photo_rejected", 1},
		{"already_done", 0},
		{"no_target", 0},
		{"success", 0},
		{"photo_missing", 0},
	}
	for _, c := range cases {
		got := BudgetFor(c.outcome)
		if got.MaxAttempts != c.want {
			t.Fatalf("BudgetFor(%s).MaxAttempts = %d, want %d", c.outcome, got.MaxAttempts, c.want)
		}
	}
}

func TestBudgetForClearsSession(t *testing.T) {
	// 会话已不可信的结论必须清会话：challenge 与凭据错。
	if !BudgetFor("challenge").ClearSession {
		t.Fatal("challenge 应清会话")
	}
	if !BudgetFor("credential_invalid").ClearSession {
		t.Fatal("credential_invalid 应清会话")
	}
	if BudgetFor("transient").ClearSession {
		t.Fatal("transient 不清会话")
	}
	// 风控拦截不代表会话失效：1 次即终态，但不改会话。
	if BudgetFor("blocked").ClearSession {
		t.Fatal("blocked 不清会话")
	}
}

func TestBudgetForUnknownWord(t *testing.T) {
	// 未知 outcome 不可能是 success：按最保守档（1 次、清会话）。
	got := BudgetFor("made_up_word")
	if got.MaxAttempts != 1 || !got.ClearSession {
		t.Fatalf("未知词档 = %#v, want 1 次＋清会话", got)
	}
}

// 词表全量对照已移到 internal/signing/conformance/retry_vocab_test.go
// （P4-A 复审 P2-1）：那里 import contract 真源遍历常量，契约改名立刻红。
// 本包不 import signing（单向依赖），只钉词档行为与未知词保守档。

// 落点判据：第 attempt 档的名义间隔按 Multiplier 放大，每档带抖动。
//
// 抖动让具体毫秒不可复算，故断言**区间**：第 n 档落在
// base×M^(n-1)×[1-f, 1+f] 内。区间是判据，具体值不是。
func TestNextDelayGrowsByAttempt(t *testing.T) {
	p := Policy{MaxAttempts: 3, After: 60 * time.Second}
	w := Window{End: farFuture}
	now := time.Unix(1000, 0)
	bounds := []struct{ low, high time.Duration }{
		{30 * time.Second, 90 * time.Second},   // 60s ×[0.5,1.5]
		{60 * time.Second, 180 * time.Second},  // 120s
		{120 * time.Second, 360 * time.Second}, // 240s
	}
	for attempt, want := range bounds {
		for i := 0; i < 32; i++ {
			d, ok := NextDelay(p, attempt+1, w, now)
			if !ok {
				t.Fatalf("第 %d 档不该放弃", attempt+1)
			}
			if d < want.low || d > want.high {
				t.Fatalf("第 %d 档 delay = %v, want [%v,%v]", attempt+1, d, want.low, want.high)
			}
		}
	}
}

// 抖动必须真的抖动：同一档重复取值不得恒等，
// 否则"打散重试落点"这个目标没达成，退化成确定性间隔。
func TestNextDelayJitters(t *testing.T) {
	p := Policy{MaxAttempts: 3, After: 60 * time.Second}
	w := Window{End: farFuture}
	now := time.Unix(1000, 0)
	seen := map[time.Duration]bool{}
	for i := 0; i < 32; i++ {
		d, ok := NextDelay(p, 1, w, now)
		if !ok {
			t.Fatal("must not give up")
		}
		seen[d] = true
	}
	if len(seen) < 2 {
		t.Fatalf("32 次取样只有 %d 个不同间隔", len(seen))
	}
}

// 档位号小于 1 时按第 1 档：脏数据不得给出零等待。
func TestNextDelayClampsAttemptFloor(t *testing.T) {
	p := Policy{MaxAttempts: 3, After: 60 * time.Second}
	for _, attempt := range []int{0, -3} {
		d, ok := NextDelay(p, attempt, Window{End: farFuture}, time.Unix(1000, 0))
		if !ok || d < 30*time.Second || d > 90*time.Second {
			t.Fatalf("attempt=%d delay = (%v,%v), want 第 1 档区间", attempt, d, ok)
		}
	}
}

func TestNextDelayClampedToWindow(t *testing.T) {
	// 窗口只剩 30s：cap = 30s×0.3 = 9s < base 60s ⇒ 放弃。
	p := Policy{MaxAttempts: 3, After: 60 * time.Second}
	w := Window{End: time.Unix(1030, 0)}
	if _, ok := NextDelay(p, 1, w, time.Unix(1000, 0)); ok {
		t.Fatal("尾段放不下一个合法间隔，应放弃")
	}
}

func TestNextDelayCapBoundary(t *testing.T) {
	// cap 恰等于 base：允许（相等不是"放不下"）。剩余 = 60s/0.3 = 200s。
	// 抖动可能给到 base 以上，此时被 cap 截到 base：落点落在 [base/2, base]。
	p := Policy{MaxAttempts: 3, After: 60 * time.Second}
	w := Window{End: time.Unix(1200, 0)}
	for i := 0; i < 32; i++ {
		d, ok := NextDelay(p, 1, w, time.Unix(1000, 0))
		if !ok || d < 30*time.Second || d > 60*time.Second {
			t.Fatalf("cap==base 应放行并截到 cap, got (%v,%v)", d, ok)
		}
	}
	// cap 比 base 少 1ns：必须放弃。这是 cap<base 判据的 1ns 边界带，
	// 突变（cap < base-1）在此存活过——审查 P1-2 的钉子。
	w2 := Window{End: time.Unix(1000, 0).Add(199*time.Second + 999*time.Millisecond + 999*time.Microsecond + 999*time.Nanosecond)}
	if _, ok := NextDelay(p, 1, w2, time.Unix(1000, 0)); ok {
		t.Fatal("cap 比 base 少 1ns 应放弃")
	}
}

func TestNextDelayTailShare(t *testing.T) {
	// 剩余 1000s：cap = min(600, 300) = 300s；第 3 档也截到 cap。
	p := Policy{MaxAttempts: 3, After: 60 * time.Second}
	w := Window{End: time.Unix(2000, 0)}
	for i := 0; i < 32; i++ {
		d, ok := NextDelay(p, 3, w, time.Unix(1000, 0))
		if !ok || d < 120*time.Second || d > 300*time.Second {
			t.Fatalf("第 3 档 delay = (%v,%v), want [120s,300s]", d, ok)
		}
	}
	// 剩余 1 小时：cap = min(600, 1080) = 600s（硬上限生效）。
	// 档位再大，曲线也被 CapSec 截住；抖动上界仍会撞到 cap。
	w2 := Window{End: time.Unix(4600, 0)}
	max := time.Duration(0)
	for i := 0; i < 64; i++ {
		d, ok := NextDelay(p, 10, w2, time.Unix(1000, 0))
		if !ok || d < 300*time.Second || d > 600*time.Second {
			t.Fatalf("硬上限档 delay = (%v,%v), want [300s,600s]", d, ok)
		}
		if d > max {
			max = d
		}
	}
	if max != 600*time.Second {
		t.Fatalf("64 次取样最大值 = %v, want 600s（cap 必须真的截住）", max)
	}
}

// 已过窗口：绝不重试（业务日窗口已过 ⇒ 放弃，不写进次日窗口）。
func TestNextDelayAfterWindow(t *testing.T) {
	p := Policy{MaxAttempts: 3, After: 60 * time.Second}
	w := Window{End: time.Unix(900, 0)}
	if _, ok := NextDelay(p, 1, w, time.Unix(1000, 0)); ok {
		t.Fatal("窗口已过不该给落点")
	}
}
