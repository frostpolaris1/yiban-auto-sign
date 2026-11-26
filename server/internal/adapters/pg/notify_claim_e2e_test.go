package pg_test

// P3-9/P3-11④ 投递认领租约的真库 e2e。
//
// 覆盖：认领参数校验；两实例并发认领互斥（id 不重叠、并集为全部到期行）；
// 租约内不可重认领、到期后可重认领（边界含 claimed_until <= now）；
// claimed_until 精确等于 now+lease；被行锁占用的行靠 SKIP LOCKED 立即跳过；
// MarkSent/MarkFailed/MarkSkipped 都清空认领，且重试行只在 next_retry_at 到期后可见。
// 读面 ListDueForSend 的语义由既有用例守着，这里不改。

import (
	"context"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
)

// claimDueRows 造 n 条同一收件人的到期 email 账本行（独立去重键），返回账本 id。
func (e *testEnv) claimDueRows(n int, due time.Time) []string {
	e.t.Helper()
	user := e.createUser(fmt.Sprintf("claim-%s@example.com", randomHex(4)))
	notes := pg.NewNotificationRepo(e.db)
	ledger := pg.NewNotificationDeliveryRepo(e.db)
	ids := make([]string, 0, n)
	for i := 0; i < n; i++ {
		note, err := notes.Insert(e.ctx, corenotify.NotificationInsert{
			UserID:    user.ID,
			Category:  corenotify.CategoryReviewPending,
			DedupeKey: fmt.Sprintf("claim/%s/%d", randomHex(4), i),
			Title:     "账号待审核",
			Body:      "账号 138****0001 已提交审核。",
			CreatedAt: due,
		})
		if err != nil {
			e.t.Fatalf("insert notification: %v", err)
		}
		row, err := ledger.InsertPending(e.ctx, corenotify.DeliveryInsert{
			NotificationID: note.ID,
			UserID:         user.ID,
			Channel:        corenotify.ChannelEmail,
			NextRetryAt:    &due,
		})
		if err != nil {
			e.t.Fatalf("insert pending: %v", err)
		}
		ids = append(ids, row.ID)
	}
	return ids
}

// claimedUntilIsNull 报告某行的 claimed_until 是否为空。
func (e *testEnv) claimedUntilIsNull(id string) bool {
	e.t.Helper()
	return e.scalarInt(
		`SELECT count(*) FROM notification_deliveries WHERE id = $1::uuid AND claimed_until IS NULL`,
		id) == 1
}

// 参数校验：now 非零、limit 1..200、lease (0,24h]；越界一律 validation。
func TestClaimDueValidatesInput(t *testing.T) {
	e := newEnv(t)
	ledger := pg.NewNotificationDeliveryRepo(e.db)
	now := time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)
	e.claimDueRows(1, now.Add(-time.Minute))

	for _, tc := range []struct {
		name  string
		now   time.Time
		limit int
		lease time.Duration
	}{
		{"zero now", time.Time{}, 1, time.Minute},
		{"limit zero", now, 0, time.Minute},
		{"limit over cap", now, 201, time.Minute},
		{"zero lease", now, 1, 0},
		{"negative lease", now, 1, -time.Second},
		{"lease over 24h", now, 1, 25 * time.Hour},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := ledger.ClaimDueForSend(e.ctx, tc.now, tc.limit, tc.lease); errs.From(err).Code != errs.CodeValidation {
				t.Fatalf("code = %v (%v), want validation", errs.From(err).Code, err)
			}
		})
	}

	// 上界 24h 本身合法（不报校验错）。
	if _, err := ledger.ClaimDueForSend(e.ctx, now, 1, 24*time.Hour); err != nil {
		t.Fatalf("lease=24h 应合法: %v", err)
	}
}

// 两个实例并发认领：各拿 1 行，id 不重叠，并集恰为 2 行到期行；
// 全部认领后再认领为空。
func TestClaimDueConcurrentInstancesExclusive(t *testing.T) {
	e := newEnv(t)
	now := time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)
	ids := e.claimDueRows(2, now.Add(-time.Minute))
	repos := []*pg.NotificationDeliveryRepo{
		pg.NewNotificationDeliveryRepo(e.db),
		pg.NewNotificationDeliveryRepo(e.db),
	}

	results := make([][]corenotify.DueDelivery, len(repos))
	failures := make([]error, len(repos))
	start := make(chan struct{})
	var wg sync.WaitGroup
	for i := range repos {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			results[i], failures[i] = repos[i].ClaimDueForSend(ctx, now, 1, time.Minute)
		}(i)
	}
	close(start)
	wg.Wait()

	seen := map[string]int{}
	for i := range repos {
		if failures[i] != nil {
			t.Fatalf("实例 %d 认领失败: %v", i, failures[i])
		}
		if len(results[i]) != 1 {
			t.Fatalf("实例 %d 认领 %d 行, want 1", i, len(results[i]))
		}
		seen[results[i][0].ID]++
	}
	if len(seen) != len(ids) {
		t.Fatalf("认领并集 = %d 行, want %d（%v）", len(seen), len(ids), seen)
	}
	for _, id := range ids {
		if seen[id] != 1 {
			t.Fatalf("账本行 %s 认领次数 = %d, want 1", id, seen[id])
		}
	}

	due, err := repos[0].ClaimDueForSend(e.ctx, now, 10, time.Minute)
	if err != nil || len(due) != 0 {
		t.Fatalf("租约内再认领 = (%d,%v), want 0", len(due), err)
	}
}

// 租约语义：claimed_until 恰为 now+lease；到期前不可再认领，
// 到期（含相等）后可再认领；认领行带通知正文与收件地址。
func TestClaimDueLeaseBoundsReclaim(t *testing.T) {
	e := newEnv(t)
	now := time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)
	id := e.claimDueRows(1, now.Add(-time.Minute))[0]
	ledger := pg.NewNotificationDeliveryRepo(e.db)
	lease := time.Minute

	first, err := ledger.ClaimDueForSend(e.ctx, now, 10, lease)
	if err != nil || len(first) != 1 {
		t.Fatalf("首轮认领 = (%d,%v), want 1", len(first), err)
	}
	row := first[0]
	if row.ID != id || row.Title != "账号待审核" || row.Body != "账号 138****0001 已提交审核。" ||
		row.Email == "" || row.Status != corenotify.DeliveryStatusPending || row.Attempts != 0 {
		t.Fatalf("认领行 = %+v", row)
	}
	if got := e.scalarInt(
		`SELECT count(*) FROM notification_deliveries WHERE id = $1::uuid AND claimed_until = $2::timestamptz`,
		id, now.Add(lease)); got != 1 {
		t.Fatalf("claimed_until != now+lease（%d 行精确匹配）", got)
	}

	for _, tc := range []struct {
		name string
		at   time.Time
		want int
	}{
		{"同一时刻", now, 0},
		{"到期前 1s", now.Add(lease - time.Second), 0},
		{"到期时刻", now.Add(lease), 1},
		{"已续租到 now+2m", now.Add(lease + time.Second), 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := ledger.ClaimDueForSend(e.ctx, tc.at, 10, lease)
			if err != nil {
				t.Fatalf("ClaimDueForSend: %v", err)
			}
			if len(got) != tc.want {
				t.Fatalf("认领 %d 行, want %d", len(got), tc.want)
			}
		})
	}
}

// 三种终态记账都清空认领：sent/skipped 退出调度；failed 只在 next_retry_at 到期后可见。
func TestClaimDueClearedByTerminalMarks(t *testing.T) {
	e := newEnv(t)
	now := time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)
	ids := e.claimDueRows(3, now.Add(-time.Minute))
	ledger := pg.NewNotificationDeliveryRepo(e.db)

	claimed, err := ledger.ClaimDueForSend(e.ctx, now, 10, time.Minute)
	if err != nil || len(claimed) != 3 {
		t.Fatalf("认领 = (%d,%v), want 3", len(claimed), err)
	}
	retry := now.Add(5 * time.Minute)
	if err := ledger.MarkSent(e.ctx, ids[0]); err != nil {
		t.Fatalf("MarkSent: %v", err)
	}
	if err := ledger.MarkSkipped(e.ctx, ids[1], "cooldown_hit"); err != nil {
		t.Fatalf("MarkSkipped: %v", err)
	}
	if err := ledger.MarkFailed(e.ctx, ids[2], "connect", &retry); err != nil {
		t.Fatalf("MarkFailed: %v", err)
	}
	for _, id := range ids {
		if !e.claimedUntilIsNull(id) {
			t.Fatalf("终态记账后认领未清空: %s", id)
		}
	}

	if due, err := ledger.ClaimDueForSend(e.ctx, now, 10, time.Minute); err != nil || len(due) != 0 {
		t.Fatalf("终态后立即认领 = (%d,%v), want 0", len(due), err)
	}
	due, err := ledger.ClaimDueForSend(e.ctx, retry.Add(time.Second), 10, time.Minute)
	if err != nil || len(due) != 1 || due[0].ID != ids[2] {
		t.Fatalf("重试到期认领 = (%+v,%v), want 仅 %s", due, err, ids[2])
	}
}

// 行锁被别的连接占住时：SKIP LOCKED 让认领立即跳过，不阻塞、不抢锁。
// 没有 SKIP LOCKED 的实现会一直等到外层事务结束（本用例 3s 超时即红）。
func TestClaimDueSkipsLockedRows(t *testing.T) {
	e := newEnv(t)
	now := time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)
	id := e.claimDueRows(1, now.Add(-time.Minute))[0]
	ledger := pg.NewNotificationDeliveryRepo(e.db)

	locked := make(chan struct{})
	release := make(chan struct{})
	txDone := make(chan error, 1)
	go func() {
		txDone <- e.db.InTx(context.Background(), func(ctx context.Context) error {
			if _, err := e.db.QueryString(ctx,
				`SELECT id::text FROM notification_deliveries WHERE id = $1::uuid FOR UPDATE`, id); err != nil {
				return err
			}
			close(locked)
			<-release
			return nil
		})
	}()
	<-locked

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	due, err := ledger.ClaimDueForSend(ctx, now, 10, time.Minute)
	cancel()
	close(release)
	if err != nil {
		t.Fatalf("被行锁占用时应跳过而非等待: %v", err)
	}
	if len(due) != 0 {
		t.Fatalf("认领到被锁行: %+v", due)
	}
	if err := <-txDone; err != nil {
		t.Fatalf("持锁事务: %v", err)
	}

	// 锁释放后同一行可正常认领。
	due, err = ledger.ClaimDueForSend(e.ctx, now, 10, time.Minute)
	if err != nil || len(due) != 1 || due[0].ID != id {
		t.Fatalf("锁释放后认领 = (%+v,%v), want %s", due, err, id)
	}
}
