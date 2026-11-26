package pg_test

// 恢复路径的属主生命周期 e2e（P1-5abc）。
//
// # 功能
// 钉住「一人一号」在恢复面同样成立：管理员恢复一个属主已软删的账号、
// 或属主名下已有 live 账号时都必须报 conflict 而不是 capacity，
// 且被拒的账号保持软删；容量满仍报 capacity。
// 自助恢复（UserRepo.RestoreSelfAccount）在属主已有 live 账号时
// 返回 (false, nil) 且不复活候选。
//
// # 归属
// adapters/pg 的仓储判据测试；HTTP 状态映射与用例接线由 cmd/server e2e 覆盖。

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// TestRestoreAdminRejectsDeletedOwner 覆盖管理员恢复已软删属主名下的账号。
// 现状 UPDATE 只查一人一号 NOT EXISTS，不查属主存活，故会复活账号；
// 目标行为是 conflict 且行保持软删。
func TestRestoreAdminRejectsDeletedOwner(t *testing.T) {
	e := newEnv(t)
	owner := e.createUser("restore-deleted-owner@example.com")
	school := e.createSchool("restore-deleted-owner")
	account := e.createAccount(school.ID, owner.ID, "13900002001")
	if _, err := e.db.Exec(e.ctx,
		"UPDATE accounts SET deleted_at = now() WHERE id = $1::uuid", account.ID); err != nil {
		t.Fatalf("soft delete account: %v", err)
	}
	if _, err := e.db.Exec(e.ctx,
		"UPDATE users SET deleted_at = now() WHERE id = $1::uuid", owner.ID); err != nil {
		t.Fatalf("soft delete owner: %v", err)
	}

	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.accts.Restore(ctx, corescope.Master(), account.ID, 10000)
		return err
	})
	if got := codeOf(t, err); got != errs.CodeConflict {
		t.Fatalf("admin restore under deleted owner: code = %q (err %v), want conflict", got, err)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NOT NULL", account.ID); got != 1 {
		t.Fatal("rejected restore revived the account row")
	}
}

// TestRestoreAdminWaitsForOwnerSoftDelete 钉住恢复与属主软删的串行化：
// 软删事务未提交时，恢复必须等属主行锁，不能凭语句快照把账号复活成
// 「已软删属主名下的活号」。
func TestRestoreAdminWaitsForOwnerSoftDelete(t *testing.T) {
	e := newEnv(t)
	owner := e.createUser("restore-race-owner@example.com")
	school := e.createSchool("restore-race-owner")
	account := e.createAccount(school.ID, owner.ID, "13900002009")
	if _, err := e.db.Exec(e.ctx,
		"UPDATE accounts SET deleted_at = now() WHERE id = $1::uuid", account.ID); err != nil {
		t.Fatalf("soft delete account: %v", err)
	}

	ready := make(chan struct{})
	release := make(chan struct{})
	ownerErr := make(chan error, 1)
	go func() {
		ownerErr <- e.db.InTx(e.ctx, func(ctx context.Context) error {
			if _, err := e.db.Exec(ctx,
				"UPDATE users SET deleted_at = now() WHERE id = $1::uuid", owner.ID); err != nil {
				return err
			}
			close(ready)
			<-release
			return nil
		})
	}()
	<-ready

	started := time.Now()
	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.accts.Restore(ctx, corescope.Master(), account.ID, 10000)
		return err
	})
	elapsed := time.Since(started)
	close(release)
	if err == nil {
		t.Fatal("restore revived an account while the owner soft delete was uncommitted")
	}
	if elapsed < time.Second {
		t.Fatalf("restore returned after %v; it did not wait for the owner row lock", elapsed)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NOT NULL", account.ID); got != 1 {
		t.Fatal("failed restore changed the account row")
	}
	if err := <-ownerErr; err != nil {
		t.Fatalf("owner soft delete: %v", err)
	}
}

// TestRestoreAdminRejectsOwnerWithLiveAccount 覆盖属主名下已有 live 账号时
// 管理员恢复另一条软删账号：必须报 conflict（一人一号），不是 capacity。
func TestRestoreAdminRejectsOwnerWithLiveAccount(t *testing.T) {
	e := newEnv(t)
	owner := e.createUser("restore-owner-live@example.com")
	school := e.createSchool("restore-owner-live")
	deletedAccount := e.createAccount(school.ID, owner.ID, "13900002002")
	liveAccount := e.createAccount(school.ID, owner.ID, "13900002003")
	if _, err := e.db.Exec(e.ctx,
		"UPDATE accounts SET deleted_at = now() WHERE id = $1::uuid", deletedAccount.ID); err != nil {
		t.Fatalf("soft delete account: %v", err)
	}

	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.accts.Restore(ctx, corescope.Master(), deletedAccount.ID, 10000)
		return err
	})
	if got := codeOf(t, err); got != errs.CodeConflict {
		t.Fatalf("admin restore with live sibling: code = %q (err %v), want conflict", got, err)
	}
	if !strings.Contains(err.Error(), "owner already has a live account") {
		t.Fatalf("error does not explain the one-live rule: %v", err)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NOT NULL", deletedAccount.ID); got != 1 {
		t.Fatal("rejected restore revived the deleted account")
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NULL", liveAccount.ID); got != 1 {
		t.Fatal("rejected restore disturbed the live sibling")
	}
}

// TestRestoreAdminCapacityStillCapacity 覆盖非结构冲突的普通容量满：
// 属主存活且名下无 live 账号，但全局 live 数已达上限，仍报 capacity。
func TestRestoreAdminCapacityStillCapacity(t *testing.T) {
	e := newEnv(t)
	owner := e.createUser("restore-cap-owner@example.com")
	other := e.createUser("restore-cap-other@example.com")
	school := e.createSchool("restore-cap")
	account := e.createAccount(school.ID, owner.ID, "13900002004")
	if _, err := e.db.Exec(e.ctx,
		"INSERT INTO accounts (school_id, owner_user_id, phone) VALUES ($1::uuid, $2::uuid, $3)",
		school.ID, other.ID, "13900002005"); err != nil {
		t.Fatalf("create live account: %v", err)
	}
	if _, err := e.db.Exec(e.ctx,
		"UPDATE accounts SET deleted_at = now() WHERE id = $1::uuid", account.ID); err != nil {
		t.Fatalf("soft delete account: %v", err)
	}

	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.accts.Restore(ctx, corescope.Master(), account.ID, 1)
		return err
	})
	if got := codeOf(t, err); got != errs.CodeCapacity {
		t.Fatalf("admin restore at capacity: code = %q (err %v), want capacity_exceeded", got, err)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NOT NULL", account.ID); got != 1 {
		t.Fatal("capacity-rejected restore revived the account")
	}
}

// TestRestoreAdminRestoresLiveOwner 是正向对照：属主存活、无 live 兄弟、
// 容量充足时恢复成功，且清空 deleted_by。
func TestRestoreAdminRestoresLiveOwner(t *testing.T) {
	e := newEnv(t)
	owner := e.createUser("restore-ok-owner@example.com")
	school := e.createSchool("restore-ok")
	account := e.createAccount(school.ID, owner.ID, "13900002006")
	if _, err := e.db.Exec(e.ctx,
		"UPDATE accounts SET deleted_at = now(), deleted_by = $2::uuid WHERE id = $1::uuid",
		account.ID, owner.ID); err != nil {
		t.Fatalf("soft delete account: %v", err)
	}

	mustNotError(t, e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.accts.Restore(ctx, corescope.Master(), account.ID, 10000)
		return err
	}), "admin restore")
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NULL AND deleted_by IS NULL", account.ID); got != 1 {
		t.Fatal("account is not live with cleared deleted_by after restore")
	}
}

// TestRestoreSelfRejectsDeletedOwner 覆盖本人自删账号的纵深守卫：
// 属主行已软删时，即使 deleted_by/时间戳都匹配也不得复活账号，
// 且零行分类必须是 conflict 而不是误导性的 capacity。
func TestRestoreSelfRejectsDeletedOwner(t *testing.T) {
	e := newEnv(t)
	owner := e.createUser("self-restore-deleted-owner@example.com")
	school := e.createSchool("self-restore-deleted-owner")
	account := e.createAccount(school.ID, owner.ID, "13900002007")
	at := selfDeleteTime()
	if _, err := e.db.Exec(e.ctx,
		"UPDATE accounts SET deleted_at = $2, deleted_by = $1::uuid WHERE id = $3::uuid",
		owner.ID, at, account.ID); err != nil {
		t.Fatalf("soft delete account: %v", err)
	}
	if _, err := e.db.Exec(e.ctx,
		"UPDATE users SET deleted_at = $2 WHERE id = $1::uuid", owner.ID, at); err != nil {
		t.Fatalf("soft delete owner: %v", err)
	}

	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.accts.RestoreSelf(ctx, corescope.Master(), account.ID, owner.ID, at.Add(-time.Hour), 10000)
		return err
	})
	if got := codeOf(t, err); got != errs.CodeConflict {
		t.Fatalf("self restore under deleted owner: code = %q (err %v), want conflict", got, err)
	}
	if !strings.Contains(err.Error(), "account owner is deleted") {
		t.Fatalf("error does not explain the deleted owner: %v", err)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NOT NULL", account.ID); got != 1 {
		t.Fatal("rejected self restore revived the account")
	}
}

// TestRestoreSelfAccountSkipsWhenOwnerLive 覆盖用户自助恢复账号时
// 属主名下已有 live 账号：返回 (false,nil)，不复活注销时刻的候选。
func TestRestoreSelfAccountSkipsWhenOwnerLive(t *testing.T) {
	e := newEnv(t)
	owner := e.createUser("self-acct-owner-live@example.com")
	school := e.createSchool("self-acct-owner-live")
	candidate := e.createAccount(school.ID, owner.ID, "13900002008")
	e.createAccount(school.ID, owner.ID, "13900002009")
	at := selfDeleteTime()
	if _, err := e.db.Exec(e.ctx,
		"UPDATE accounts SET deleted_at = $2, deleted_by = $1::uuid WHERE id = $3::uuid",
		owner.ID, at, candidate.ID); err != nil {
		t.Fatalf("soft delete candidate: %v", err)
	}

	var restored bool
	mustNotError(t, e.db.InTx(e.ctx, func(ctx context.Context) error {
		var err error
		restored, err = e.users.RestoreSelfAccount(ctx, owner.ID, at, 10000)
		return err
	}), "self account restore with live sibling")
	if restored {
		t.Fatal("RestoreSelfAccount reported a restore under a live sibling")
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NOT NULL", candidate.ID); got != 1 {
		t.Fatal("candidate was revived despite the one-live rule")
	}
}

// TestRestoreSelfAccountNoCandidate 覆盖无候选：返回 (false, nil)，不报错。
func TestRestoreSelfAccountNoCandidate(t *testing.T) {
	e := newEnv(t)
	owner := e.createUser("self-acct-none@example.com")

	var restored bool
	mustNotError(t, e.db.InTx(e.ctx, func(ctx context.Context) error {
		var err error
		restored, err = e.users.RestoreSelfAccount(ctx, owner.ID, selfDeleteTime(), 10000)
		return err
	}), "self account restore without candidate")
	if restored {
		t.Fatal("RestoreSelfAccount reported a restore without a candidate")
	}
}

// TestRestoreSelfAccountCapacityStillCapacity 覆盖容量满仍报 capacity：
// 属主无 live 号，但全局 live 数已达上限。
func TestRestoreSelfAccountCapacityStillCapacity(t *testing.T) {
	e := newEnv(t)
	owner := e.createUser("self-acct-cap@example.com")
	other := e.createUser("self-acct-cap-other@example.com")
	school := e.createSchool("self-acct-cap")
	candidate := e.createAccount(school.ID, owner.ID, "13900002010")
	e.createAccount(school.ID, other.ID, "13900002011")
	at := selfDeleteTime()
	if _, err := e.db.Exec(e.ctx,
		"UPDATE accounts SET deleted_at = $2, deleted_by = $1::uuid WHERE id = $3::uuid",
		owner.ID, at, candidate.ID); err != nil {
		t.Fatalf("soft delete candidate: %v", err)
	}

	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.users.RestoreSelfAccount(ctx, owner.ID, at, 1)
		return err
	})
	if got := codeOf(t, err); got != errs.CodeCapacity {
		t.Fatalf("self account restore at capacity: code = %q (err %v), want capacity_exceeded", got, err)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NOT NULL", candidate.ID); got != 1 {
		t.Fatal("capacity-rejected self restore revived the candidate")
	}
}

// TestRestoreSelfAccountRestoresFreeOwner 是正向对照：属主无 live 号、
// 容量充足时恢复候选并清空 deleted_by。
func TestRestoreSelfAccountRestoresFreeOwner(t *testing.T) {
	e := newEnv(t)
	owner := e.createUser("self-acct-ok@example.com")
	school := e.createSchool("self-acct-ok")
	candidate := e.createAccount(school.ID, owner.ID, "13900002012")
	at := selfDeleteTime()
	if _, err := e.db.Exec(e.ctx,
		"UPDATE accounts SET deleted_at = $2, deleted_by = $1::uuid WHERE id = $3::uuid",
		owner.ID, at, candidate.ID); err != nil {
		t.Fatalf("soft delete candidate: %v", err)
	}

	var restored bool
	mustNotError(t, e.db.InTx(e.ctx, func(ctx context.Context) error {
		var err error
		restored, err = e.users.RestoreSelfAccount(ctx, owner.ID, at, 10000)
		return err
	}), "self account restore")
	if !restored {
		t.Fatal("RestoreSelfAccount did not restore the candidate")
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NULL AND deleted_by IS NULL", candidate.ID); got != 1 {
		t.Fatal("candidate is not live with cleared deleted_by")
	}
}
