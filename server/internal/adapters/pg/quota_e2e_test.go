package pg_test

// 配额 0=不限哨兵的仓储层真库 e2e（r4r）。
//
// 断言：limit=0 时全局计数判据整体跳过（不是数到 0 就全拒）；
// 一人一号（per-owner live≤1）是独立结构判据，0 不影响；
// 负数不是合法上限，报 validation_failed。
// 用例链（resolver → usecase → repo）与 HTTP 写回在 cmd/server 的
// quota_e2e_test.go 覆盖；本文件只钉仓储判据。

import (
	"context"
	"testing"
	"time"

	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// 仓储层 0 哨兵：limit=0 跳过计数判据；负数仍是构造错误。
func TestQuotaRepoZeroSentinelE2E(t *testing.T) {
	e := newEnv(t)
	// 用户线：limit=0 下连续建行不受限；limit=-1 报校验错。
	for i, email := range []string{"zero-a@example.com", "zero-b@example.com"} {
		if err := e.db.InTx(e.ctx, func(ctx context.Context) error {
			_, err := e.users.Create(ctx, coreuser.New{
				Email: email, PasswordHash: "argon2id$test", Role: coreauth.RoleUser,
			}, 0)
			return err
		}); err != nil {
			t.Fatalf("user %d under unlimited: %v", i, err)
		}
	}
	if err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.users.Create(ctx, coreuser.New{
			Email: "zero-neg@example.com", PasswordHash: "argon2id$test", Role: coreauth.RoleUser,
		}, -1)
		return err
	}); err == nil || errs.From(err).Code != errs.CodeValidation {
		t.Fatalf("negative limit err = %v, want validation_failed", err)
	}

	// 账号线：limit=0 下两个属主各建一号；同一属主第二号仍 conflict。
	school := e.createSchool("zero-sentinel")
	ownerA := e.createUser("zero-owner-a@example.com")
	ownerB := e.createUser("zero-owner-b@example.com")
	create := func(ownerID, phone string) error {
		return e.db.InTx(context.Background(), func(ctx context.Context) error {
			_, err := e.accts.Create(ctx, coreaccount.New{
				SchoolID: school.ID, OwnerUserID: ownerID, Phone: phone,
			}, 0)
			return err
		})
	}
	if err := create(ownerA.ID, "13800003001"); err != nil {
		t.Fatalf("owner A under unlimited: %v", err)
	}
	if err := create(ownerB.ID, "13800003002"); err != nil {
		t.Fatalf("owner B under unlimited: %v", err)
	}
	if err := create(ownerA.ID, "13800003003"); err == nil || errs.From(err).Code != errs.CodeConflict {
		t.Fatalf("same-owner second account err = %v, want conflict", err)
	}
	if err := e.db.InTx(context.Background(), func(ctx context.Context) error {
		_, err := e.accts.Create(ctx, coreaccount.New{
			SchoolID: school.ID, OwnerUserID: ownerB.ID, Phone: "13800003004",
		}, -1)
		return err
	}); err == nil || errs.From(err).Code != errs.CodeValidation {
		t.Fatalf("negative account limit err = %v, want validation_failed", err)
	}
}

// 恢复路径的 0=不限：Restore / RestoreSelfUser / RestoreSelfAccount /
// AccountRepo.Restore / AccountRepo.RestoreSelf 在 limit=0 时都跳过
// 全局计数判据（存量 live 行多于 0 也放行）；负数仍报校验错。
func TestQuotaRepoZeroSentinelRestoreE2E(t *testing.T) {
	e := newEnv(t)
	school := e.createSchool("zero-restore")

	// 用户线：keeper 占一个 live 名额，gone 软删后由管理员恢复。
	keeper := e.createUser("zero-restore-keeper@example.com")
	gone := e.createUser("zero-restore-gone@example.com")
	if _, err := e.db.Exec(e.ctx,
		`UPDATE users SET deleted_at = now() WHERE id = $1::uuid`, gone.ID); err != nil {
		t.Fatalf("soft delete user: %v", err)
	}
	if err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.users.Restore(ctx, gone.ID, 0)
		return err
	}); err != nil {
		t.Fatalf("user Restore with limit=0 while 1 live user exists: %v", err)
	}
	if _, err := e.db.Exec(e.ctx,
		`UPDATE users SET deleted_at = now() WHERE id = $1::uuid`, gone.ID); err != nil {
		t.Fatalf("soft delete user again: %v", err)
	}
	if err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.users.Restore(ctx, gone.ID, -1)
		return err
	}); err == nil || errs.From(err).Code != errs.CodeValidation {
		t.Fatalf("negative restore limit err = %v, want validation_failed", err)
	}

	// 自助恢复用户（auth 自助注销路径）：deleted_by=本人，limit=0 放行。
	if _, err := e.db.Exec(e.ctx,
		`UPDATE users SET deleted_at = now(), deleted_by = id WHERE id = $1::uuid`, gone.ID); err != nil {
		t.Fatalf("self soft delete user: %v", err)
	}
	notBefore := time.Now().Add(-time.Hour)
	if err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.users.RestoreSelfUser(ctx, gone.ID, notBefore, 0)
		return err
	}); err != nil {
		t.Fatalf("RestoreSelfUser with limit=0 while 1 live user exists: %v", err)
	}

	// 账号线：keeper 的账号占一个 live 名额；gone 的账号软删后管理员恢复。
	keeperAccount := e.createAccount(school.ID, keeper.ID, "13500020001")
	goneAccount := e.createAccount(school.ID, gone.ID, "13500020002")
	if _, err := e.db.Exec(e.ctx,
		`UPDATE accounts SET deleted_at = now() WHERE id = $1::uuid`, goneAccount.ID); err != nil {
		t.Fatalf("soft delete account: %v", err)
	}
	if err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.accts.Restore(ctx, corescope.Master(), goneAccount.ID, 0)
		return err
	}); err != nil {
		t.Fatalf("account Restore with limit=0 while 1 live account exists: %v", err)
	}
	if err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.accts.Restore(ctx, corescope.Master(), keeperAccount.ID, -1)
		return err
	}); err == nil || errs.From(err).Code != errs.CodeValidation {
		t.Fatalf("negative account restore limit err = %v, want validation_failed", err)
	}

	// 自助恢复账号：固定 deleted_at 供 RestoreSelfAccount 精确匹配。
	deletedAt := time.Date(2026, 11, 10, 12, 0, 0, 0, time.UTC)
	if _, err := e.db.Exec(e.ctx, `
		UPDATE accounts SET deleted_at = $2, deleted_by = $3::uuid WHERE id = $1::uuid`,
		goneAccount.ID, deletedAt, gone.ID); err != nil {
		t.Fatalf("self soft delete account: %v", err)
	}
	if err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.accts.RestoreSelf(ctx, corescope.User(gone.ID), goneAccount.ID,
			gone.ID, deletedAt.Add(-time.Hour), 0)
		return err
	}); err != nil {
		t.Fatalf("account RestoreSelf with limit=0 while 1 live account exists: %v", err)
	}
	if _, err := e.db.Exec(e.ctx, `
		UPDATE accounts SET deleted_at = $2, deleted_by = $3::uuid WHERE id = $1::uuid`,
		goneAccount.ID, deletedAt, gone.ID); err != nil {
		t.Fatalf("self soft delete account again: %v", err)
	}
	if err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.users.RestoreSelfAccount(ctx, gone.ID, deletedAt, 0)
		return err
	}); err != nil {
		t.Fatalf("RestoreSelfAccount with limit=0 while 1 live account exists: %v", err)
	}
}
