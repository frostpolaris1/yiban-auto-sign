package pg_test

// 登录账号自助注销/恢复的仓储级 e2e。
//
// # 功能
// 钉住 SQL 面的四条判据：注销连带软删名下账号、恢复只取注销同一时刻
// 且 deleted_by=本人的账号、超窗用户恢复报 not_found、活跃用户容量在
// 锁下重新计数并报 capacity。真 PostgreSQL，不用替身。
//
// # 归属
// adapters/pg 的仓储行为测试；HTTP 与用例接线由 cmd/server e2e 覆盖。

import (
	"context"
	"testing"
	"time"

	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// selfDeleteTime 返回可安全往返 timestamptz 的当前时刻。
func selfDeleteTime() time.Time {
	return coreclock.System{}.Now().UTC().Truncate(time.Microsecond)
}

// TestSoftDeleteSelfCascadesAccounts 覆盖注销用户与名下账号的连带软删。
func TestSoftDeleteSelfCascadesAccounts(t *testing.T) {
	e := newEnv(t)
	owner := e.createUser("self-cascade@example.com")
	school := e.createSchool("self-cascade")
	account := e.createAccount(school.ID, owner.ID, "13900139000")
	at := selfDeleteTime()

	var cascaded int
	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		var err error
		cascaded, err = e.users.SoftDeleteSelf(ctx, owner.ID, at)
		return err
	})
	mustNotError(t, err, "soft delete self")
	if cascaded != 1 {
		t.Fatalf("cascaded accounts = %d, want 1", cascaded)
	}
	if _, err := e.users.GetByID(e.ctx, owner.ID); codeOf(t, err) != errs.CodeNotFound {
		t.Fatalf("live user lookup after self-delete: want not_found, got %v", err)
	}
	deleted, err := e.users.FindDeletedByEmail(e.ctx, owner.Email)
	mustNotError(t, err, "find deleted by email")
	if deleted.DeletedAt == nil || !deleted.DeletedAt.Equal(at) {
		t.Fatalf("user deleted_at = %v, want %v", deleted.DeletedAt, at)
	}
	if got := e.scalarString(
		"SELECT coalesce(deleted_by::text, '') FROM accounts WHERE id = $1::uuid", account.ID); got != owner.ID {
		t.Fatalf("account deleted_by = %q, want %q", got, owner.ID)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at = $2", account.ID, at); got != 1 {
		t.Fatal("account deleted_at does not match the user row")
	}
}

// TestRestoreSelfRestoresCascadeAccount 覆盖用户与账号一并恢复，
// 以及时间戳不匹配的账号不被恢复。
func TestRestoreSelfRestoresCascadeAccount(t *testing.T) {
	e := newEnv(t)
	owner := e.createUser("self-restore@example.com")
	school := e.createSchool("self-restore")
	account := e.createAccount(school.ID, owner.ID, "13900139001")
	at := selfDeleteTime()
	mustNotError(t, e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.users.SoftDeleteSelf(ctx, owner.ID, at)
		return err
	}), "soft delete self")

	var restoredOwner coreuser.Record
	var restoredAccount bool
	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		locked, err := e.users.GetDeleted(ctx, owner.ID)
		if err != nil {
			return err
		}
		restoredOwner, err = e.users.RestoreSelfUser(ctx, locked.ID, at.Add(-coreuser.SelfRestoreGrace), 10000)
		if err != nil {
			return err
		}
		restoredAccount, err = e.users.RestoreSelfAccount(ctx, restoredOwner.ID, at, 10000)
		return err
	})
	mustNotError(t, err, "restore self")
	if !restoredAccount {
		t.Fatal("cascade account was not restored")
	}
	if restoredOwner.DeletedAt != nil {
		t.Fatal("restored user still carries deleted_at")
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NULL", account.ID); got != 1 {
		t.Fatal("account is not live after restore")
	}
	if got := e.scalarString(
		"SELECT coalesce(deleted_by::text, '') FROM accounts WHERE id = $1::uuid", account.ID); got != "" {
		t.Fatalf("account deleted_by after restore = %q, want empty", got)
	}

	// 同一账号已恢复；再用错误时间戳调用必须无候选、不报错。
	mustNotError(t, e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.users.SoftDeleteSelf(ctx, owner.ID, at.Add(time.Hour))
		return err
	}), "soft delete self again")
	mustNotError(t, e.db.InTx(e.ctx, func(ctx context.Context) error {
		restored, err := e.users.RestoreSelfUser(ctx, owner.ID, at.Add(-coreuser.SelfRestoreGrace), 10000)
		if err != nil {
			return err
		}
		var ok bool
		ok, err = e.users.RestoreSelfAccount(ctx, restored.ID, at, 10000)
		if err != nil {
			return err
		}
		if ok {
			t.Fatal("account with a different cascade timestamp was restored")
		}
		return nil
	}), "wrong timestamp restore")
}

// TestHasSelfDeletedEmailWithinScansAllRows 钉住「不能只看最近一条」：
// 最近一条是管理员删除，较早一条是本人宽限内删除，仍必须拦住注册。
func TestHasSelfDeletedEmailWithinScansAllRows(t *testing.T) {
	e := newEnv(t)
	const email = "self-scan@example.com"
	admin := e.createUser("self-scan-admin@example.com")
	self := e.createUser(email)
	now := coreclock.System{}.Now().UTC()
	mustNotError(t, e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.users.SoftDeleteSelf(ctx, self.ID, now.Add(-6*24*time.Hour))
		return err
	}), "self delete")
	// 同邮箱再建一行再管理员删除：它比本人删除更新，FindDeletedByEmail 会选中它。
	other := e.createUser(email)
	mustNotError(t, e.users.SoftDelete(e.ctx, other.ID, admin.ID, now.Add(-24*time.Hour)), "admin delete")

	latest, err := e.users.FindDeletedByEmail(e.ctx, email)
	mustNotError(t, err, "find latest deleted")
	if latest.ID != other.ID || latest.DeletedBy != admin.ID {
		t.Fatalf("latest deleted row = %+v, want admin-deleted %s", latest, other.ID)
	}

	var blocked bool
	mustNotError(t, e.db.InTx(e.ctx, func(ctx context.Context) error {
		if err := e.users.LockEmail(ctx, email); err != nil {
			return err
		}
		var err error
		blocked, err = e.users.HasSelfDeletedEmailWithin(ctx, email, now.Add(-coreuser.SelfRestoreGrace))
		return err
	}), "check grace rows")
	if !blocked {
		t.Fatal("EXISTS missed the older self-deleted row within grace")
	}

	// 本人行超窗后，管理员删除的行不占邮箱。
	if _, err := e.db.Exec(e.ctx,
		"UPDATE users SET deleted_at = $2 WHERE id = $1::uuid",
		self.ID, now.Add(-coreuser.SelfRestoreGrace-time.Hour)); err != nil {
		t.Fatalf("backdate self delete: %v", err)
	}
	mustNotError(t, e.db.InTx(e.ctx, func(ctx context.Context) error {
		if err := e.users.LockEmail(ctx, email); err != nil {
			return err
		}
		var err error
		blocked, err = e.users.HasSelfDeletedEmailWithin(ctx, email, now.Add(-coreuser.SelfRestoreGrace))
		return err
	}), "check grace rows after expiry")
	if blocked {
		t.Fatal("expired self-deleted row still blocks registration")
	}
}

// TestRestoreSelfRejectsExpiredUser 覆盖超 7 天的用户恢复报 not_found。
func TestRestoreSelfRejectsExpiredUser(t *testing.T) {
	e := newEnv(t)
	owner := e.createUser("self-expired@example.com")
	at := selfDeleteTime().Add(-coreuser.SelfRestoreGrace - time.Hour)
	mustNotError(t, e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.users.SoftDeleteSelf(ctx, owner.ID, at)
		return err
	}), "soft delete self")

	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.users.RestoreSelfUser(ctx, owner.ID, coreclock.System{}.Now().UTC().Add(-coreuser.SelfRestoreGrace), 10000)
		return err
	})
	if codeOf(t, err) != errs.CodeNotFound {
		t.Fatalf("expired restore: want not_found, got %v", err)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM users WHERE id = $1::uuid AND deleted_at IS NOT NULL", owner.ID); got != 1 {
		t.Fatal("expired restore revived the user")
	}
}

// TestRestoreSelfUserRespectsCapacity 覆盖活跃用户上限在恢复时重新计数。
func TestRestoreSelfUserRespectsCapacity(t *testing.T) {
	e := newEnv(t)
	live := e.createUser("self-cap-live@example.com")
	deleted := e.createUser("self-cap-deleted@example.com")
	at := selfDeleteTime()
	mustNotError(t, e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.users.SoftDeleteSelf(ctx, deleted.ID, at)
		return err
	}), "soft delete deleted user")

	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.users.RestoreSelfUser(ctx, deleted.ID, at.Add(-coreuser.SelfRestoreGrace), 1)
		return err
	})
	if codeOf(t, err) != errs.CodeCapacity {
		t.Fatalf("restore at capacity: want capacity_exceeded, got %v", err)
	}

	// 腾出一个活跃名额后恢复成功。
	mustNotError(t, e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.users.SoftDeleteSelf(ctx, live.ID, at)
		return err
	}), "soft delete live user")
	var restored coreuser.Record
	mustNotError(t, e.db.InTx(e.ctx, func(ctx context.Context) error {
		var err error
		restored, err = e.users.RestoreSelfUser(ctx, deleted.ID, at.Add(-coreuser.SelfRestoreGrace), 1)
		return err
	}), "restore after freeing a slot")
	if restored.ID != deleted.ID || restored.DeletedAt != nil {
		t.Fatalf("restored user = %+v", restored)
	}
}
