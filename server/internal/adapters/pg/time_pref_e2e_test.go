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
	if err := repo.Put(e.ctx, user.ID, &offset, now); err != nil {
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
	if err := repo.Put(e.ctx, user.ID, &later, now.Add(time.Minute)); err != nil {
		t.Fatalf("put again: %v", err)
	}
	pref, err = repo.Get(e.ctx, user.ID)
	if err != nil {
		t.Fatalf("get again: %v", err)
	}
	if pref.OffsetMin == nil || *pref.OffsetMin != 45 {
		t.Fatalf("覆盖失败: %+v", pref)
	}

	if err := repo.Put(e.ctx, user.ID, nil, now.Add(2*time.Minute)); err != nil {
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

// 批量读取跳过 NULL 与无行用户；非法粒度必须被库约束拒绝。
func TestTimePrefRepoOffsets(t *testing.T) {
	e := newEnv(t)
	repo := pg.NewTimePrefRepo(e.db)
	withPref := e.createUser("pref-b@example.com")
	cleared := e.createUser("pref-c@example.com")
	missing := e.createUser("pref-d@example.com")
	now := time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)
	offset := 60
	if err := repo.Put(e.ctx, withPref.ID, &offset, now); err != nil {
		t.Fatalf("put: %v", err)
	}
	if err := repo.Put(e.ctx, cleared.ID, nil, now); err != nil {
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
	if err := repo.Put(e.ctx, missing.ID, &bad, now); err == nil {
		t.Fatal("非 5 分钟粒度应被拒绝")
	}
}
