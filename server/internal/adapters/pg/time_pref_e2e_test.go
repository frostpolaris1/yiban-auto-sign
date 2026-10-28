package pg_test

// time_prefs 仓储 e2e：缺行、写入、清除与批量读取。
// 依赖真实 PostgreSQL 的 ON CONFLICT 与 CHECK 约束。

import (
	"errors"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

// 写入覆盖同一行；清除写 NULL；缺行返回 not_found。
func TestTimePrefRepoUpsertAndClear(t *testing.T) {
	e := newEnv(t)
	repo := pg.NewTimePrefRepo(e.db)
	user := e.createUser("pref-a@example.com")
	now := time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)

	if _, err := repo.Get(e.ctx, user.ID); !errors.Is(err, errs.ErrNotFound) {
		t.Fatalf("缺行错误 = %v, want not_found", err)
	}
	offset := 30
	if _, err := repo.PutWithin(e.ctx, user.ID, &offset, now, 0); err != nil {
		t.Fatalf("put: %v", err)
	}
	pref, err := repo.Get(e.ctx, user.ID)
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if pref.OffsetMin == nil || *pref.OffsetMin != 30 || !pref.UpdatedAt.Equal(now) {
		t.Fatalf("pref = %+v", pref)
	}

	later := 45
	if _, err := repo.PutWithin(e.ctx, user.ID, &later, now.Add(time.Minute), 0); err != nil {
		t.Fatalf("put again: %v", err)
	}
	pref, err = repo.Get(e.ctx, user.ID)
	if err != nil {
		t.Fatalf("get again: %v", err)
	}
	if pref.OffsetMin == nil || *pref.OffsetMin != 45 {
		t.Fatalf("覆盖失败: %+v", pref)
	}

	if _, err := repo.PutWithin(e.ctx, user.ID, nil, now.Add(2*time.Minute), 0); err != nil {
		t.Fatalf("clear: %v", err)
	}
	pref, err = repo.Get(e.ctx, user.ID)
	if err != nil {
		t.Fatalf("get cleared: %v", err)
	}
	if pref.OffsetMin != nil {
		t.Fatalf("清除后仍有偏移: %+v", pref)
	}
}

// k0y-3：冷却判定与写入是同一条语句——冷却内第二次写零行、值不变；
// 冷却归零后放行。并发写只有一个能落由同一条 WHERE 保证（单行 UPSERT）。
func TestTimePrefRepoPutWithinCooldown(t *testing.T) {
	e := newEnv(t)
	repo := pg.NewTimePrefRepo(e.db)
	user := e.createUser("pref-cool@example.com")
	now := time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)

	first := 30
	if ok, err := repo.PutWithin(e.ctx, user.ID, &first, now, time.Minute); err != nil || !ok {
		t.Fatalf("首次写入 = ok=%v err=%v", ok, err)
	}
	second := 45
	if ok, err := repo.PutWithin(e.ctx, user.ID, &second, now.Add(30*time.Second), time.Minute); err != nil || ok {
		t.Fatalf("冷却内写入 = ok=%v err=%v, want ok=false", ok, err)
	}
	pref, err := repo.Get(e.ctx, user.ID)
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if pref.OffsetMin == nil || *pref.OffsetMin != 30 {
		t.Fatalf("冷却内写入改了值：%+v", pref)
	}
	if !pref.UpdatedAt.Equal(now) {
		t.Fatalf("冷却内写入改了 updated_at：%v, want %v", pref.UpdatedAt, now)
	}
	if ok, err := repo.PutWithin(e.ctx, user.ID, &second, now.Add(2*time.Minute), time.Minute); err != nil || !ok {
		t.Fatalf("冷却过后写入 = ok=%v err=%v, want ok=true", ok, err)
	}
	third := 15
	if ok, err := repo.PutWithin(e.ctx, user.ID, &third, now.Add(3*time.Minute), 0); err != nil || !ok {
		t.Fatalf("冷却归零应放行 = ok=%v err=%v", ok, err)
	}
	pref, err = repo.Get(e.ctx, user.ID)
	if err != nil {
		t.Fatalf("get after cooldown-zero write: %v", err)
	}
	if pref.OffsetMin == nil || *pref.OffsetMin != 15 {
		t.Fatalf("冷却归零写入未生效：%+v", pref)
	}
}

// k0y-3 并发面：两个并发 PutWithin 只能有一个落（条件 UPSERT 的原子性）。
// 用同一起跑栅栏把两次写压在一起；断言恰好一个 ok、最终值属于胜者。
func TestTimePrefRepoPutWithinConcurrentSingleWinner(t *testing.T) {
	e := newEnv(t)
	repo := pg.NewTimePrefRepo(e.db)
	user := e.createUser("pref-race@example.com")
	now := time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)

	start := make(chan struct{})
	type outcome struct {
		ok     bool
		err    error
		offset int
	}
	results := make(chan outcome, 2)
	for _, offset := range []int{30, 45} {
		offset := offset
		go func() {
			<-start
			ok, err := repo.PutWithin(e.ctx, user.ID, &offset, now, time.Minute)
			results <- outcome{ok: ok, err: err, offset: offset}
		}()
	}
	close(start)
	winners := 0
	winnerOffset := 0
	for i := 0; i < 2; i++ {
		got := <-results
		if got.err != nil {
			t.Fatalf("并发写错误：%v", got.err)
		}
		if got.ok {
			winners++
			winnerOffset = got.offset
		}
	}
	if winners != 1 {
		t.Fatalf("并发写胜者 = %d, want 恰好 1", winners)
	}
	pref, err := repo.Get(e.ctx, user.ID)
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if pref.OffsetMin == nil || *pref.OffsetMin != winnerOffset {
		t.Fatalf("最终值 = %+v, want 胜者的 %d", pref, winnerOffset)
	}
}

// 批量读取跳过 NULL 与无行用户；非法粒度必须被库约束拒绝。
func TestTimePrefRepoOffsets(t *testing.T) {
	e := newEnv(t)
	repo := pg.NewTimePrefRepo(e.db)
	withPref := e.createUser("pref-b@example.com")
	cleared := e.createUser("pref-c@example.com")
	missing := e.createUser("pref-d@example.com")
	now := time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)
	offset := 60
	if _, err := repo.PutWithin(e.ctx, withPref.ID, &offset, now, 0); err != nil {
		t.Fatalf("put: %v", err)
	}
	if _, err := repo.PutWithin(e.ctx, cleared.ID, nil, now, 0); err != nil {
		t.Fatalf("put nil: %v", err)
	}
	got, err := repo.Offsets(e.ctx, []string{withPref.ID, cleared.ID, missing.ID})
	if err != nil {
		t.Fatalf("offsets: %v", err)
	}
	if len(got) != 1 || got[withPref.ID] != 60 {
		t.Fatalf("offsets = %v", got)
	}

	bad := 7
	if _, err := repo.PutWithin(e.ctx, missing.ID, &bad, now, 0); err == nil {
		t.Fatal("非 5 分钟粒度应被拒绝")
	}
}
