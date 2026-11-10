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

	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
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
