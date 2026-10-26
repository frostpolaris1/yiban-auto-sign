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

// 落点判据：窗口宽裕时取基础间隔；last×3 参与放大但不超过 cap。
func TestNextDelayWithinWindow(t *testing.T) {
	p := Policy{MaxAttempts: 3, After: 60 * time.Second}
	w := Window{End: farFuture}
	d, ok := NextDelay(p, 0, w, time.Unix(1000, 0))
	if !ok || d != 60*time.Second {
		t.Fatalf("NextDelay = (%v,%v), want (60s,true)", d, ok)
	}
	// last=100s ⇒ max(base, 300s) = 300s，仍在 cap 内。
	d, ok = NextDelay(p, 100*time.Second, w, time.Unix(1000, 0))
	if !ok || d != 300*time.Second {
		t.Fatalf("NextDelay(last=100s) = (%v,%v), want (300s,true)", d, ok)
	}
}

func TestNextDelayClampedToWindow(t *testing.T) {
	// 窗口只剩 30s：cap = 30s×0.3 = 9s < base 60s ⇒ 放弃。
	p := Policy{MaxAttempts: 3, After: 60 * time.Second}
	w := Window{End: time.Unix(1030, 0)}
	if _, ok := NextDelay(p, 0, w, time.Unix(1000, 0)); ok {
		t.Fatal("尾段放不下一个合法间隔，应放弃")
	}
}

func TestNextDelayCapBoundary(t *testing.T) {
	// cap 恰等于 base：允许（相等不是"放不下"）。剩余 = 60s/0.3 = 200s。
	p := Policy{MaxAttempts: 3, After: 60 * time.Second}
	w := Window{End: time.Unix(1200, 0)}
	d, ok := NextDelay(p, 0, w, time.Unix(1000, 0))
	if !ok || d != 60*time.Second {
		t.Fatalf("cap==base 应放行 60s, got (%v,%v)", d, ok)
	}
	// cap 比 base 少 1ns：必须放弃。这是 cap<base 判据的 1ns 边界带，
	// 突变（cap < base-1）在此存活过——审查 P1-2 的钉子。
	w2 := Window{End: time.Unix(1000, 0).Add(199*time.Second + 999*time.Millisecond + 999*time.Microsecond + 999*time.Nanosecond)}
	if _, ok := NextDelay(p, 0, w2, time.Unix(1000, 0)); ok {
		t.Fatal("cap 比 base 少 1ns 应放弃")
	}
}

func TestNextDelayTailShare(t *testing.T) {
	// 剩余 1000s：cap = min(600, 300) = 300s；last 很大也只批到 cap。
	p := Policy{MaxAttempts: 3, After: 60 * time.Second}
	w := Window{End: time.Unix(2000, 0)}
	d, ok := NextDelay(p, 400*time.Second, w, time.Unix(1000, 0))
	if !ok || d != 300*time.Second {
		t.Fatalf("NextDelay = (%v,%v), want (300s,true)", d, ok)
	}
	// 剩余 1 小时：cap = min(600, 1080) = 600s（硬上限生效）。
	w2 := Window{End: time.Unix(4600, 0)}
	d, ok = NextDelay(p, 1<<20*time.Second, w2, time.Unix(1000, 0))
	if !ok || d != 600*time.Second {
		t.Fatalf("NextDelay = (%v,%v), want (600s,true)", d, ok)
	}
}

// 已过窗口：绝不重试（业务日窗口已过 ⇒ 放弃，不写进次日窗口）。
func TestNextDelayAfterWindow(t *testing.T) {
	p := Policy{MaxAttempts: 3, After: 60 * time.Second}
	w := Window{End: time.Unix(900, 0)}
	if _, ok := NextDelay(p, 0, w, time.Unix(1000, 0)); ok {
		t.Fatal("窗口已过不该给落点")
	}
}
