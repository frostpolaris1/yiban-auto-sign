package pg_test

// P3-9/P3-11④：认领释放与不可见行的真库 e2e。
//
// 覆盖：ReleaseClaim 只清 claimed_until（状态/尝试数不动），下一轮同一时刻
// 即可重新认领；重复释放与未认领行是空操作；空 id 走 validation；
// next_retry_at 在未来的行不会被认领（N7）。

import (
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

// ReleaseClaim 释放后同一 now 可立即重领，且不误清别的行。
func TestReleaseClaimMakesRowImmediatelyClaimable(t *testing.T) {
	e := newEnv(t)
	ledger := pg.NewNotificationDeliveryRepo(e.db)
	now := time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)
	ids := e.claimDueRows(2, now.Add(-time.Minute))

	claimed, err := ledger.ClaimDueForSend(e.ctx, now, 10, time.Minute)
	if err != nil || len(claimed) != 2 {
		t.Fatalf("认领 = (%d,%v), want 2", len(claimed), err)
	}
	if err := ledger.ReleaseClaim(e.ctx, ids[0]); err != nil {
		t.Fatalf("ReleaseClaim: %v", err)
	}
	if !e.claimedUntilIsNull(ids[0]) {
		t.Fatalf("释放后 claimed_until 非空")
	}
	if e.claimedUntilIsNull(ids[1]) {
		t.Fatalf("释放误清另一行的 claimed_until")
	}
	// 释放只清租约：行仍是 pending、attempts 不变，next_retry_at 原样保留。
	if got := e.scalarString(
		"SELECT status FROM notification_deliveries WHERE id = $1::uuid", ids[0]); got != "pending" {
		t.Fatalf("释放后 status = %q, want pending", got)
	}
	if got := e.scalarInt(
		"SELECT attempts FROM notification_deliveries WHERE id = $1::uuid", ids[0]); got != 0 {
		t.Fatalf("释放后 attempts = %d, want 0（释放不是尝试）", got)
	}

	again, err := ledger.ClaimDueForSend(e.ctx, now, 10, time.Minute)
	if err != nil || len(again) != 1 || again[0].ID != ids[0] {
		t.Fatalf("释放后再认领 = (%d,%v), want 仅 %s", len(again), err, ids[0])
	}

	// 重复释放与未认领行都是空操作。
	if err := ledger.ReleaseClaim(e.ctx, ids[0]); err != nil {
		t.Fatalf("二次 ReleaseClaim: %v", err)
	}
	if err := ledger.ReleaseClaim(e.ctx, "00000000-0000-0000-0000-000000000000"); err != nil {
		t.Fatalf("未认领行 ReleaseClaim: %v", err)
	}
	if err := ledger.ReleaseClaim(e.ctx, ""); errs.From(err).Code != errs.CodeValidation {
		t.Fatalf("空 id code = %v (%v), want validation", errs.From(err).Code, err)
	}
}

// next_retry_at 在未来的行不参与本轮认领（即使行本身已过租约）。
func TestClaimDueSkipsFutureRetry(t *testing.T) {
	e := newEnv(t)
	ledger := pg.NewNotificationDeliveryRepo(e.db)
	now := time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)
	dueID := e.claimDueRows(1, now.Add(-time.Minute))[0]
	futureID := e.claimDueRows(1, now.Add(time.Hour))[0]
	// 租约取 2h：第二轮的 now+1h 仍在第一轮到期行的租约内，
	// 这样第二轮若只认领到未来行，就证明是 next_retry_at 拦住了它，
	// 而不是租约到期放回来的。
	lease := 2 * time.Hour

	got, err := ledger.ClaimDueForSend(e.ctx, now, 10, lease)
	if err != nil || len(got) != 1 || got[0].ID != dueID {
		t.Fatalf("到期认领 = (%d,%v), want 仅 %s", len(got), err, dueID)
	}
	if !e.claimedUntilIsNull(futureID) {
		t.Fatalf("未来 next_retry_at 的行被认领")
	}

	got, err = ledger.ClaimDueForSend(e.ctx, now.Add(time.Hour+time.Second), 10, lease)
	if err != nil || len(got) != 1 || got[0].ID != futureID {
		t.Fatalf("到期后认领 = (%d,%v), want 仅 %s", len(got), err, futureID)
	}
}
