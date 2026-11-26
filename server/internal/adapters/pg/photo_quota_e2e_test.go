package pg_test

// photos 每用户滚动 24 小时上传配额 e2e：advisory lock 串行化、
// 重复摘要不受配额限制、软删行仍占配额、窗口外旧行不计、必须事务内运行。
// 依赖真实 PostgreSQL：hashtextextended、pg_advisory_xact_lock 与
// READ COMMITTED 下的锁后计数没有诚实的进程内替身。

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	corephoto "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/photo"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// createWithinQuota 在独立事务内调用配额写入，返回结果与错误。
func createWithinQuota(e *testEnv, repo *pg.PhotoRepo, rec corephoto.Record, since time.Time, limit int) (corephoto.Record, error) {
	var out corephoto.Record
	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		var err error
		out, err = repo.CreateWithinQuota(ctx, rec, since, limit)
		return err
	})
	return out, err
}

// 配额判据：limit 内成功；新摘要超限 ErrDailyUploadQuota；
// 重复摘要达配额后仍成功且恢复软删行；软删不释放配额。
func TestPhotoRepoCreateWithinQuota(t *testing.T) {
	e := newEnv(t)
	repo := pg.NewPhotoRepo(e.db)
	u := e.createUser("photo-quota@example.com")
	now := clock.System{}.Now().UTC()
	limit := 2
	since := now.Add(-24 * time.Hour)

	first, err := createWithinQuota(e, repo, photoRecord(u.ID, 1, now), since, limit)
	if err != nil {
		t.Fatalf("第一张: %v", err)
	}
	if _, err := createWithinQuota(e, repo, photoRecord(u.ID, 2, now), since, limit); err != nil {
		t.Fatalf("第二张: %v", err)
	}
	if _, err := createWithinQuota(e, repo, photoRecord(u.ID, 3, now), since, limit); !errors.Is(err, corephoto.ErrDailyUploadQuota) {
		t.Fatalf("第三张错误 = %v, want ErrDailyUploadQuota", err)
	}

	// 达配额后重传首张：重复摘要不占新配额，返回同一行且未软删。
	again, err := createWithinQuota(e, repo, photoRecord(u.ID, 1, now.Add(time.Minute)), since, limit)
	if err != nil {
		t.Fatalf("达配额后重传: %v", err)
	}
	if again.ID != first.ID || again.DeletedAt != nil {
		t.Fatalf("重传 = %+v, want id %s 且未软删", again, first.ID)
	}

	// 软删第一张后仍占配额：删图不能绕过滚动窗口。
	if _, _, err := repo.SoftDelete(e.ctx, corescope.User(u.ID), first.ID, now); err != nil {
		t.Fatalf("软删: %v", err)
	}
	if _, err := createWithinQuota(e, repo, photoRecord(u.ID, 4, now), since, limit); !errors.Is(err, corephoto.ErrDailyUploadQuota) {
		t.Fatalf("软删后新摘要 = %v, want ErrDailyUploadQuota", err)
	}

	// 第二张仍在库；窗口内行数恰为 limit（软删行计入）。
	if n := e.scalarInt(`SELECT count(*) FROM photos WHERE owner_user_id = $1::uuid AND created_at >= $2`,
		u.ID, since); n != int64(limit) {
		t.Fatalf("窗口内行数 = %d, want %d", n, limit)
	}
}

// 窗口判据：25 小时前的不同摘要不占当前滚动 24 小时配额。
func TestPhotoRepoQuotaExcludesRowsOutsideWindow(t *testing.T) {
	e := newEnv(t)
	repo := pg.NewPhotoRepo(e.db)
	u := e.createUser("photo-quota-window@example.com")
	now := clock.System{}.Now().UTC()
	if _, err := repo.Create(e.ctx, photoRecord(u.ID, 10, now.Add(-25*time.Hour))); err != nil {
		t.Fatalf("旧行: %v", err)
	}
	rec, err := createWithinQuota(e, repo, photoRecord(u.ID, 11, now), now.Add(-24*time.Hour), 1)
	if err != nil {
		t.Fatalf("窗口外旧行不应占配额: %v", err)
	}
	if rec.ID == "" {
		t.Fatal("新摘要未落库")
	}
}

// 并发：limit=3，6 个 goroutine 各带不同摘要、各自独立事务同时写。
// advisory lock 应按 owner 串行化；成功数必须恰为 3，其余全是配额拒绝。
// 若 2s lock_timeout 造成超时，只能增大时限/减少并发，不得放宽断言。
func TestPhotoRepoQuotaConcurrent(t *testing.T) {
	e := newEnv(t)
	repo := pg.NewPhotoRepo(e.db)
	u := e.createUser("photo-quota-concurrent@example.com")
	now := clock.System{}.Now().UTC()
	const limit = 3
	const attempts = 6

	var (
		wg    sync.WaitGroup
		mu    sync.Mutex
		ok    int
		quota int
		other []error
	)
	for i := 0; i < attempts; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_, err := createWithinQuota(e, repo, photoRecord(u.ID, byte(100+i), now),
				now.Add(-24*time.Hour), limit)
			mu.Lock()
			defer mu.Unlock()
			switch {
			case err == nil:
				ok++
			case errors.Is(err, corephoto.ErrDailyUploadQuota):
				quota++
			default:
				other = append(other, err)
			}
		}(i)
	}
	wg.Wait()
	if ok != limit || quota != attempts-limit || len(other) != 0 {
		t.Fatalf("并发结果 = 成功 %d / 配额 %d / 其他 %v, want %d / %d / 0",
			ok, quota, other, limit, attempts-limit)
	}
}

// 非事务调用必须失败关闭：锁、计数与写入不同事务就没有配额正确性。
func TestPhotoRepoQuotaRequiresTransaction(t *testing.T) {
	e := newEnv(t)
	repo := pg.NewPhotoRepo(e.db)
	u := e.createUser("photo-quota-notx@example.com")
	now := clock.System{}.Now().UTC()
	_, err := repo.CreateWithinQuota(e.ctx, photoRecord(u.ID, 1, now), now.Add(-24*time.Hour), 1)
	if !errors.Is(err, pg.ErrNoTransaction) {
		t.Fatalf("非事务调用错误 = %v, want ErrNoTransaction", err)
	}
}
