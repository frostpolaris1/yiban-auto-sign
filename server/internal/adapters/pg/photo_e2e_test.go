package pg_test

// photos 仓储 e2e：去重恢复、作用域、保留期、清理与游标分页。
// 依赖真实 PostgreSQL 的唯一键、CHECK 与 make_interval。

import (
	"crypto/sha256"
	"errors"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corephoto "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/photo"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

func photoRecord(owner string, seed byte, at time.Time) corephoto.Record {
	var sha [32]byte
	sum := sha256.Sum256([]byte{seed})
	copy(sha[:], sum[:])
	return corephoto.Record{
		OwnerUserID: owner,
		SHA256:      sha,
		Mime:        "image/jpeg",
		Width:       8,
		Height:      8,
		Bytes:       1024,
		CreatedAt:   at,
	}
}

// 去重、恢复与作用域：同键第二次写恢复软删行；非 user 作用域读不到。
func TestPhotoRepoDedupeAndScope(t *testing.T) {
	e := newEnv(t)
	repo := pg.NewPhotoRepo(e.db)
	u1 := e.createUser("photo-a@example.com")
	u2 := e.createUser("photo-b@example.com")
	now := clock.System{}.Now().UTC()

	first, err := repo.Create(e.ctx, photoRecord(u1.ID, 1, now))
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	again, err := repo.Create(e.ctx, photoRecord(u1.ID, 1, now.Add(time.Minute)))
	if err != nil {
		t.Fatalf("create again: %v", err)
	}
	if first.ID != again.ID {
		t.Fatalf("去重失败: %s vs %s", first.ID, again.ID)
	}
	if !again.CreatedAt.Equal(now.Add(time.Minute).Truncate(time.Microsecond)) {
		t.Fatalf("重写应重置 created_at, got %v", again.CreatedAt)
	}

	got, err := repo.Get(e.ctx, corescope.User(u1.ID), first.ID)
	if err != nil || got.ID != first.ID {
		t.Fatalf("get owner: %+v, %v", got, err)
	}
	for name, scope := range map[string]corescope.Scope{
		"other user": corescope.User(u2.ID),
		"admin":      corescope.Admin([]string{u1.ID}),
		"master":     corescope.Master(),
	} {
		if _, err := repo.Get(e.ctx, scope, first.ID); !errors.Is(err, errs.ErrNotFound) {
			t.Fatalf("%s get = %v, want not_found", name, err)
		}
	}

	deleted, err := repo.SoftDelete(e.ctx, corescope.User(u1.ID), first.ID, now.Add(2*time.Minute))
	if err != nil || deleted.DeletedAt == nil {
		t.Fatalf("delete: %+v, %v", deleted, err)
	}
	if _, err := repo.Get(e.ctx, corescope.User(u1.ID), first.ID); !errors.Is(err, errs.ErrNotFound) {
		t.Fatalf("删除后 get = %v, want not_found", err)
	}
	// 跨用户软删必须 not_found，且不得改动原行。
	if _, err := repo.SoftDelete(e.ctx, corescope.User(u2.ID), first.ID, now.Add(2*time.Minute)); !errors.Is(err, errs.ErrNotFound) {
		t.Fatalf("跨用户软删 = %v, want not_found", err)
	}
	if left := e.scalarInt("SELECT count(*) FROM photos WHERE id = $1::uuid AND deleted_at IS NOT NULL", first.ID); left != 1 {
		t.Fatalf("跨用户软删改动了原行, rows=%d", left)
	}
	restored, err := repo.Create(e.ctx, photoRecord(u1.ID, 1, now.Add(3*time.Minute)))
	if err != nil {
		t.Fatalf("restore: %v", err)
	}
	if restored.ID != first.ID || restored.DeletedAt != nil {
		t.Fatalf("未恢复同一行: %+v", restored)
	}
}

// 列表只返回本人的未删行，按 created_at 倒序，游标可续页。
func TestPhotoRepoListPaging(t *testing.T) {
	e := newEnv(t)
	repo := pg.NewPhotoRepo(e.db)
	u1 := e.createUser("photo-page-a@example.com")
	u2 := e.createUser("photo-page-b@example.com")
	base := clock.System{}.Now().UTC().Truncate(time.Microsecond)
	for i, at := range []time.Time{base.Add(-2 * time.Hour), base.Add(-time.Hour), base} {
		if _, err := repo.Create(e.ctx, photoRecord(u1.ID, byte(i+10), at)); err != nil {
			t.Fatalf("create %d: %v", i, err)
		}
	}
	if _, err := repo.Create(e.ctx, photoRecord(u2.ID, 20, base)); err != nil {
		t.Fatalf("create other: %v", err)
	}

	page1, cursor, err := repo.List(e.ctx, corescope.User(u1.ID), "", 2)
	if err != nil {
		t.Fatalf("list page1: %v", err)
	}
	if len(page1) != 2 || cursor == "" {
		t.Fatalf("page1 = %d rows, cursor=%q", len(page1), cursor)
	}
	if !page1[0].CreatedAt.After(page1[1].CreatedAt) {
		t.Fatalf("排序不是倒序: %v < %v", page1[0].CreatedAt, page1[1].CreatedAt)
	}
	page2, cursor2, err := repo.List(e.ctx, corescope.User(u1.ID), cursor, 2)
	if err != nil {
		t.Fatalf("list page2: %v", err)
	}
	if len(page2) != 1 || cursor2 != "" {
		t.Fatalf("page2 = %d rows, cursor=%q", len(page2), cursor2)
	}
	if page2[0].ID == page1[0].ID || page2[0].ID == page1[1].ID {
		t.Fatal("分页重复行")
	}

	for name, scope := range map[string]corescope.Scope{
		"admin":  corescope.Admin([]string{u1.ID}),
		"master": corescope.Master(),
	} {
		if _, _, err := repo.List(e.ctx, scope, "", 10); !errors.Is(err, errs.ErrNotFound) {
			t.Fatalf("%s list = %v, want not_found", name, err)
		}
	}
	if _, _, err := repo.List(e.ctx, corescope.User(u1.ID), "not-a-cursor", 10); err == nil {
		t.Fatal("非法游标应被拒绝")
	}
}

// 保留期缺省 0（用例补 90）；写入闭集值；库层拒绝闭集外值。
func TestPhotoRepoRetention(t *testing.T) {
	e := newEnv(t)
	repo := pg.NewPhotoRepo(e.db)
	u1 := e.createUser("photo-ret@example.com")
	now := clock.System{}.Now().UTC()

	days, err := repo.Retention(e.ctx, u1.ID)
	if err != nil || days != 0 {
		t.Fatalf("缺省值 = %d, %v", days, err)
	}
	if err := repo.PutRetention(e.ctx, u1.ID, 180, now); err != nil {
		t.Fatalf("put: %v", err)
	}
	if days, err = repo.Retention(e.ctx, u1.ID); err != nil || days != 180 {
		t.Fatalf("读取 = %d, %v", days, err)
	}
	err = repo.PutRetention(e.ctx, u1.ID, 45, now)
	if err == nil {
		t.Fatal("闭集外值应被拒绝")
	}
	if code := errs.From(err).Code; code != errs.CodeValidation {
		t.Fatalf("错误码 = %q, want validation", code)
	}
}

// 清理按各用户保留期：30 天用户的 40 天前图被删；
// 未到期图与缺省 90 天用户的 40 天前图保留。
func TestPhotoRepoPurgeExpired(t *testing.T) {
	e := newEnv(t)
	repo := pg.NewPhotoRepo(e.db)
	u30 := e.createUser("photo-purge-30@example.com")
	u90 := e.createUser("photo-purge-90@example.com")
	now := clock.System{}.Now().UTC()
	if err := repo.PutRetention(e.ctx, u30.ID, 30, now); err != nil {
		t.Fatalf("retention: %v", err)
	}
	old := now.Add(-40 * 24 * time.Hour)
	expired, err := repo.Create(e.ctx, photoRecord(u30.ID, 31, old))
	if err != nil {
		t.Fatalf("create expired: %v", err)
	}
	fresh, err := repo.Create(e.ctx, photoRecord(u30.ID, 32, now.Add(-5*24*time.Hour)))
	if err != nil {
		t.Fatalf("create fresh: %v", err)
	}
	kept, err := repo.Create(e.ctx, photoRecord(u90.ID, 33, old))
	if err != nil {
		t.Fatalf("create kept: %v", err)
	}
	// 软删行不参与物理清理：重传恢复同一 id 的语义依赖它。
	soft, err := repo.Create(e.ctx, photoRecord(u30.ID, 34, old))
	if err != nil {
		t.Fatalf("create soft: %v", err)
	}
	if _, err := repo.SoftDelete(e.ctx, corescope.User(u30.ID), soft.ID, now); err != nil {
		t.Fatalf("soft delete: %v", err)
	}

	n, err := repo.PurgeExpired(e.ctx, now, 100)
	if err != nil {
		t.Fatalf("purge: %v", err)
	}
	if n != 1 {
		t.Fatalf("删除行数 = %d, want 1", n)
	}
	if _, err := repo.Get(e.ctx, corescope.User(u30.ID), expired.ID); !errors.Is(err, errs.ErrNotFound) {
		t.Fatalf("到期行仍存在: %v", err)
	}
	if _, err := repo.Get(e.ctx, corescope.User(u30.ID), fresh.ID); err != nil {
		t.Fatalf("未到期行被删: %v", err)
	}
	if _, err := repo.Get(e.ctx, corescope.User(u90.ID), kept.ID); err != nil {
		t.Fatalf("缺省 90 天行被删: %v", err)
	}
	if left := e.scalarInt("SELECT count(*) FROM photos WHERE id = $1::uuid", soft.ID); left != 1 {
		t.Fatalf("软删行被物理清除, rows=%d", left)
	}
}

// created_at 纳秒被库截到微秒后仍可稳定读回，游标不抖。
func TestPhotoRepoCreatedAtPrecision(t *testing.T) {
	e := newEnv(t)
	repo := pg.NewPhotoRepo(e.db)
	u1 := e.createUser("photo-prec@example.com")
	at := clock.System{}.Now().UTC()
	rec, err := repo.Create(e.ctx, photoRecord(u1.ID, 41, at))
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	got, err := repo.Get(e.ctx, corescope.User(u1.ID), rec.ID)
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if !got.CreatedAt.Equal(rec.CreatedAt) {
		t.Fatalf("created_at 不一致: %v vs %v", got.CreatedAt, rec.CreatedAt)
	}
}
