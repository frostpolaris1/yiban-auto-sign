package assembly

// 执行面限速器与容量公式同源（OCR C2 P1）。
//
// 容量公式与每出口 GCRA 必须读同一个配置字段。只调容量一边时，
// 缺口门会放行执行面跑不完的计划：行按更密的节奏排进窗口，
// 执行时却被限速器反复回排（wait_rate），尾段账号落空。
// 判据：耗尽突发额度后，下一次放行的等待 = 1/rate。

import (
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
)

func TestEgressGateRateFollowsConfig(t *testing.T) {
	now := time.Unix(1_000_000, 0)
	cases := []struct {
		rate float64
		want time.Duration
	}{
		{0.2, 5 * time.Second},
		{1.0, time.Second},
		{4.0, 250 * time.Millisecond},
	}
	for _, c := range cases {
		gate := buildEgressGate(&config.Config{EgressRate: c.rate})
		admitted := 0
		for gate.Admit("e1", now) {
			admitted++
			if admitted > 100 {
				t.Fatalf("rate=%v: 限速器不收敛", c.rate)
			}
		}
		if admitted != egressBurst {
			t.Fatalf("rate=%v: 突发额度 = %d 次, want %d", c.rate, admitted, egressBurst)
		}
		if got := gate.RetryAfter("e1", now); got != c.want {
			t.Fatalf("rate=%v: RetryAfter = %v, want %v", c.rate, got, c.want)
		}
	}
}
