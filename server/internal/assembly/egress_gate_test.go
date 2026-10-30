package assembly

// 执行面限速器与容量公式同源（OCR C2 P1）。
//
// 容量公式与每出口 GCRA 必须读同一个配置字段。只调容量一边时，
// 缺口门会放行执行面跑不完的计划：行按更密的节奏排进窗口，
// 执行时却被限速器反复回排（wait_rate），尾段账号落空。
// 判据落在 buildWorkerDeps 上：它就是设置 `Limiter:` 的那一行，
// 只测 buildEgressGate 会漏掉接线（OCR C2 二轮 P2）。

import (
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
)

func TestWorkerDepsLimiterFollowsConfig(t *testing.T) {
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
		deps := buildWorkerDeps(&config.Config{EgressRate: c.rate}, workerDeps{})
		if deps.Limiter == nil {
			t.Fatalf("rate=%v: 装配结果没有限速器", c.rate)
		}
		admitted := 0
		for deps.Limiter.Admit("e1", now) {
			admitted++
			if admitted > 100 {
				t.Fatalf("rate=%v: 限速器不收敛", c.rate)
			}
		}
		if admitted != egressBurst {
			t.Fatalf("rate=%v: 突发额度 = %d 次, want %d", c.rate, admitted, egressBurst)
		}
		if got := deps.Limiter.RetryAfter("e1", now); got != c.want {
			t.Fatalf("rate=%v: RetryAfter = %v, want %v", c.rate, got, c.want)
		}
	}
}
