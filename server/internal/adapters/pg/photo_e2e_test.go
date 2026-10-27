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
	if code := codeOf(t, err); code != errs.CodeValidation {
		t.Fatalf("错误码 = %q, want validation", code)
	}
}

// 清理按各用户保留期执行。判据是 COALESCE(deleted_at, created_at)：
// 软删行从删除时刻起算保留期，未到期不物理删。
func TestPhotoRepoPurgeExpired(t *testing.T) {
	e := newEnv(t)
	repo := pg.NewPhotoRepo(e.db)
	u30 := e.createUser("photo-purge-30@example.com")
	u90 := e.createUser("photo-purge-90@example.com")
	u365 := e.createUser("photo-purge-365@example.com")
	now := clock.System{}.Now().UTC()
	for id, days := range map[string]int{u30.ID: 30, u365.ID: 365} {
		if err := repo.PutRetention(e.ctx, id, days, now); err != nil {
			t.Fatalf("retention %d: %v", days, err)
		}
	}

	expired := mustPhoto(t, repo, e, photoRecord(u30.ID, 31, now.Add(-31*24*time.Hour)))
	fresh := mustPhoto(t, repo, e, photoRecord(u30.ID, 32, now.Add(-5*24*time.Hour)))
	keptDefault := mustPhoto(t, repo, e, photoRecord(u90.ID, 33, now.Add(-40*24*time.Hour)))
	keptLong := mustPhoto(t, repo, e, photoRecord(u365.ID, 34, now.Add(-60*24*time.Hour)))
	softOld := mustPhoto(t, repo, e, photoRecord(u30.ID, 35, now.Add(-90*24*time.Hour)))
	softNew := mustPhoto(t, repo, e, photoRecord(u30.ID, 36, now.Add(-90*24*time.Hour)))
	if _, err := repo.SoftDelete(e.ctx, corescope.User(u30.ID), softOld.ID, now.Add(-31*24*time.Hour)); err != nil {
		t.Fatalf("soft delete old: %v", err)
	}
	if _, err := repo.SoftDelete(e.ctx, corescope.User(u30.ID), softNew.ID, now.Add(-24*time.Hour)); err != nil {
		t.Fatalf("soft delete new: %v", err)
	}

	n, err := repo.PurgeExpired(e.ctx, now, 100)
	if err != nil {
		t.Fatalf("purge: %v", err)
	}
	if n != 2 {
		t.Fatalf("删除行数 = %d, want 2", n)
	}
	for name, id := range map[string]string{
		"到期行":      expired.ID,
		"软删 31 天前": softOld.ID,
	} {
		if left := e.scalarInt("SELECT count(*) FROM photos WHERE id = $1::uuid", id); left != 0 {
			t.Fatalf("%s 未被物理删除, rows=%d", name, left)
		}
	}
	for name, id := range map[string]string{
		"未到期行":     fresh.ID,
		"缺省 90 天行": keptDefault.ID,
		"365 天保留行": keptLong.ID,
		"软删 1 天前行": softNew.ID,
	} {
		if left := e.scalarInt("SELECT count(*) FROM photos WHERE id = $1::uuid", id); left != 1 {
			t.Fatalf("%s 被误删, rows=%d", name, left)
		}
	}
}

// PurgeExpired 一次最多删 limit 行；调用方循环到 0 才是清空。
func TestPhotoRepoPurgeExpiredBatch(t *testing.T) {
	e := newEnv(t)
	repo := pg.NewPhotoRepo(e.db)
	u := e.createUser("photo-purge-batch@example.com")
	now := clock.System{}.Now().UTC()
	if err := repo.PutRetention(e.ctx, u.ID, 30, now); err != nil {
		t.Fatalf("retention: %v", err)
	}
	for i := 0; i < 3; i++ {
		mustPhoto(t, repo, e, photoRecord(u.ID, byte(60+i), now.Add(-40*24*time.Hour)))
	}
	for i, want := range []int64{1, 1, 1, 0} {
		n, err := repo.PurgeExpired(e.ctx, now, 1)
		if err != nil {
			t.Fatalf("purge %d: %v", i, err)
		}
		if n != want {
			t.Fatalf("第 %d 次删除行数 = %d, want %d", i+1, n, want)
		}
	}
}

// ReferencedDigests 覆盖全部行，含未到期的软删行。
// blob 回收只信本集合；漏掉软删行会把可恢复的行指向空文件。
func TestPhotoRepoReferencedDigests(t *testing.T) {
	e := newEnv(t)
	repo := pg.NewPhotoRepo(e.db)
	u1 := e.createUser("photo-ref-a@example.com")
	u2 := e.createUser("photo-ref-b@example.com")
	now := clock.System{}.Now().UTC()

	live := mustPhoto(t, repo, e, photoRecord(u1.ID, 70, now))
	soft := mustPhoto(t, repo, e, photoRecord(u2.ID, 71, now))
	if _, err := repo.SoftDelete(e.ctx, corescope.User(u2.ID), soft.ID, now); err != nil {
		t.Fatalf("soft delete: %v", err)
	}

	got, err := repo.ReferencedDigests(e.ctx)
	if err != nil {
		t.Fatalf("ReferencedDigests: %v", err)
	}
	if len(got) != 2 {
		t.Fatalf("摘要数 = %d, want 2", len(got))
	}
	for name, sha := range map[string][32]byte{"live": live.SHA256, "soft": soft.SHA256} {
		if _, ok := got[sha]; !ok {
			t.Fatalf("%s 摘要不在引用集合内", name)
		}
	}
	absent := photoRecord(u1.ID, 72, now).SHA256
	if _, ok := got[absent]; ok {
		t.Fatal("未入库的摘要出现在引用集合内")
	}
}

// mustPhoto 写入一行；失败即终止用例。
func mustPhoto(t *testing.T, repo *pg.PhotoRepo, e *testEnv, rec corephoto.Record) corephoto.Record {
	t.Helper()
	out, err := repo.Create(e.ctx, rec)
	if err != nil {
		t.Fatalf("create photo: %v", err)
	}
	return out
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

// 存储侧选图排序是确定性不变量的另一半：LibraryAt 必须按
// (created_at, id) 升序取第 offset 张。删掉 ORDER BY 时本测试必红
// （P5-C 审查 P2-2）。
func TestPhotoRepoLibraryAtOrder(t *testing.T) {
	e := newEnv(t)
	repo := pg.NewPhotoRepo(e.db)
	u1 := e.createUser("photo-order@example.com")
	base := clock.System{}.Now().UTC().Truncate(time.Microsecond)
	old := photoRecord(u1.ID, 51, base.Add(-2*time.Hour))
	new := photoRecord(u1.ID, 52, base)
	for _, rec := range []corephoto.Record{new, old} { // 故意乱序写入
		if _, err := repo.Create(e.ctx, rec); err != nil {
			t.Fatalf("create: %v", err)
		}
	}
	first, err := repo.LibraryAt(e.ctx, u1.ID, 0)
	if err != nil {
		t.Fatalf("at 0: %v", err)
	}
	second, err := repo.LibraryAt(e.ctx, u1.ID, 1)
	if err != nil {
		t.Fatalf("at 1: %v", err)
	}
	if first.SHA256 != old.SHA256 || second.SHA256 != new.SHA256 {
		t.Fatalf("排序错误: first=%x second=%x, want old→new",
			first.SHA256, second.SHA256)
	}
	if _, err := repo.LibraryAt(e.ctx, u1.ID, 2); !errors.Is(err, errs.ErrNotFound) {
		t.Fatalf("越界 = %v, want not_found", err)
	}
}
