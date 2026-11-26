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
	"testing"

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
