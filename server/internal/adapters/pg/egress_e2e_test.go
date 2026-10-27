package pg_test

import (
	"errors"
	"fmt"
	"sync"
	"testing"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

// 本文件是出口粘性绑定的 e2e 判据（P4-D）。
// 需要真实 PostgreSQL：主键收敛与 CTE 单语句没有诚实替身。

// seedEgress 建一个出口并返回 ID。
func (e *testEnv) seedEgress(identity string, enabled bool) string {
	e.t.Helper()
	id, err := e.db.QueryString(e.ctx,
		`INSERT INTO egresses (identity, enabled) VALUES ($1, $2) RETURNING id::text`,
		identity, enabled)
	if err != nil {
		e.t.Fatalf("insert egress %s: %v", identity, err)
	}
	return id
}

// ListEnabled：只列启用，按 ID 升序。
func TestEgressListEnabled(t *testing.T) {
	e := newEnv(t)
	repo := pg.NewEgressRepo(e.db)
	enabledA := e.seedEgress("https://a.example", true)
	disabled := e.seedEgress("https://b.example", false)
	enabledC := e.seedEgress("https://c.example", true)

	list, err := repo.ListEnabled(e.ctx)
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(list) != 2 {
		t.Fatalf("启用出口数 = %d, want 2", len(list))
	}
	for _, x := range list {
		if x.ID == disabled {
			t.Fatal("停用出口出现在清单")
		}
	}
	// 升序：按 uuid 十六进制序比较。
	if list[0].ID > list[1].ID {
		t.Fatalf("未按 ID 升序: %v", list)
	}
	seen := map[string]bool{list[0].ID: true, list[1].ID: true}
	if !seen[enabledA] || !seen[enabledC] {
		t.Fatalf("启用出口缺失: %v", list)
	}
}

// AssignOrGet：首配写入；再配返回既有值（粘性，不覆盖）。
func TestEgressAssignSticky(t *testing.T) {
	e := newEnv(t)
	repo := pg.NewEgressRepo(e.db)
	school := e.createSchool("egress-sticky")
	acct := e.createAccount(school.ID, e.createUser("sticky@example.com").ID, "13800010101")
	e1 := e.seedEgress("https://one.example", true)
	e2 := e.seedEgress("https://two.example", true)

	got, err := repo.AssignOrGet(e.ctx, acct.ID, e1)
	if err != nil || got != e1 {
		t.Fatalf("first assign = (%s,%v), want %s", got, err, e1)
	}
	got, err = repo.AssignOrGet(e.ctx, acct.ID, e2)
	if err != nil || got != e1 {
		t.Fatalf("second assign = (%s,%v), want sticky %s", got, err, e1)
	}
	if id, err := repo.GetAssignment(e.ctx, acct.ID); err != nil || id != e1 {
		t.Fatalf("get = (%s,%v), want %s", id, err, e1)
	}
}

// 并发首配：单条 upsert 由主键收敛，全部调用读同一绑定。
// 判别力来自**轮次**：连接池 MaxConns=4 封住真并发，
// 单账号 8 goroutine 对 DO NOTHING+SELECT 变异只有 ~70% 检出率
// （2026-10-26 审查实测 14/20 复现）。故 20 账号 × 32 goroutine。
func TestEgressAssignConcurrentSingle(t *testing.T) {
	e := newEnv(t)
	repo := pg.NewEgressRepo(e.db)
	school := e.createSchool("egress-race")
	e1 := e.seedEgress("https://race1.example", true)
	e2 := e.seedEgress("https://race2.example", true)
	for round := 0; round < 20; round++ {
		acct := e.createAccount(school.ID,
			e.createUser(fmt.Sprintf("race-%d@example.com", round)).ID,
			fmt.Sprintf("138%08d", round))
		results := make(chan string, 32)
		var wg sync.WaitGroup
		for i := 0; i < 32; i++ {
			pref := e1
			if i%2 == 1 {
				pref = e2
			}
			wg.Add(1)
			go func(pref string) {
				defer wg.Done()
				id, err := repo.AssignOrGet(e.ctx, acct.ID, pref)
				if err != nil {
					t.Errorf("round %d assign: %v", round, err)
					return
				}
				results <- id
			}(pref)
		}
		wg.Wait()
		close(results)
		var first string
		for id := range results {
			if first == "" {
				first = id
				continue
			}
			if id != first {
				t.Fatalf("round %d 并发绑定分叉: %s vs %s", round, first, id)
			}
		}
	}
}

// 无绑定：errs.ErrNotFound。
func TestEgressAssignmentNotFound(t *testing.T) {
	e := newEnv(t)
	repo := pg.NewEgressRepo(e.db)
	school := e.createSchool("egress-none")
	acct := e.createAccount(school.ID, e.createUser("none@example.com").ID, "13800010303")
	if _, err := repo.GetAssignment(e.ctx, acct.ID); !errors.Is(err, coreerrs.ErrNotFound) {
		t.Fatalf("无绑定应 not_found, got %v", err)
	}
}

// 坏账号外键：插入点映射 not_found（schema §2.4）。
func TestEgressAssignBadAccount(t *testing.T) {
	e := newEnv(t)
	repo := pg.NewEgressRepo(e.db)
	e1 := e.seedEgress("https://fk.example", true)
	_, err := repo.AssignOrGet(e.ctx, "00000000-0000-0000-0000-000000000000", e1)
	// 插入点外键失败映射为 *errs.Error（CodeNotFound），不是裸哨兵。
	if got := codeOf(t, err); got != coreerrs.CodeNotFound {
		t.Fatalf("坏账号应 not_found, got %v (%v)", got, err)
	}
}

// Get 必须能取到停用出口：粘性绑定不因运维停用而失真（复审 P2-1）。
func TestEgressGetDisabled(t *testing.T) {
	e := newEnv(t)
	repo := pg.NewEgressRepo(e.db)
	id := e.seedEgress("https://disabled.example", false)
	rec, err := repo.Get(e.ctx, id)
	if err != nil {
		t.Fatalf("get disabled: %v", err)
	}
	if rec.ID != id || rec.Identity != "https://disabled.example" {
		t.Fatalf("停用出口记录失真: %+v", rec)
	}
}
