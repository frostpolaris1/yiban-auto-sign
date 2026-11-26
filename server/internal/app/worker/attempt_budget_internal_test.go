package worker

import (
	"testing"
	"time"
)

// 结算余量必须盖住一次结算写入，否则租约封顶后仍可能被回收器抢跑。
func TestAttemptLeaseReserveCoversSettlement(t *testing.T) {
	if attemptLeaseReserve <= settlementTimeout {
		t.Fatalf("attemptLeaseReserve = %v, 必须大于 settlementTimeout = %v", attemptLeaseReserve, settlementTimeout)
	}
}

// 配置预算只在租约余量内生效：租约从认领起算，预算从凭据就绪起算。
// leaseSpent 表示前序已吃掉整段租约：调用方不得再发平台请求。
func TestEffectiveAttemptBudgetCappedByLease(t *testing.T) {
	now := time.Date(2026, 10, 29, 7, 0, 0, 0, time.UTC)
	lease := now.Add(10 * time.Minute)
	cases := []struct {
		name       string
		configured time.Duration
		lease      time.Time
		want       time.Duration
		wantSpent  bool
	}{
		{"no-config-no-limit", 0, lease, 0, false},
		{"no-config-lease-spent", 0, now.Add(-time.Second), 0, true},
		{"no-lease-keeps-config", 2 * time.Minute, time.Time{}, 2 * time.Minute, false},
		{"config-inside-lease", 2 * time.Minute, lease, 2 * time.Minute, false},
		{"config-beyond-lease", 20 * time.Minute, lease, 10*time.Minute - attemptLeaseReserve, false},
		{"lease-already-spent", 2 * time.Minute, now.Add(-time.Second), 0, true},
		{"lease-tail-smaller-than-reserve", 2 * time.Minute, now.Add(500 * time.Millisecond), 0, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, spent := effectiveAttemptBudget(tc.configured, tc.lease, now)
			if got != tc.want || spent != tc.wantSpent {
				t.Fatalf("effectiveAttemptBudget = (%v, %v), want (%v, %v)", got, spent, tc.want, tc.wantSpent)
			}
		})
	}
}
