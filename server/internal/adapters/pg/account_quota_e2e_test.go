package pg_test

// 一人一号结构不变量的 e2e：每个属主名下最多一个 live 账号，
// 对全部建号路径生效；宽限期内的软删行不计 live。
// 依赖真实 PostgreSQL；计数、插入与 advisory lock 同事务。

import (
	"context"
	"strings"
	"testing"
	"time"

	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

func TestOwnerSingleAccountInvariant(t *testing.T) {
	e := newEnv(t)
	school := e.createSchool("oneacct")
	owner := e.createUser("oneacct-owner@example.com")
	other := e.createUser("oneacct-other@example.com")

	create := func(ownerID, phone string) error {
		return e.db.InTx(context.Background(), func(ctx context.Context) error {
			_, err := e.accts.Create(ctx, coreaccount.New{
				SchoolID: school.ID, OwnerUserID: ownerID, Phone: phone,
			}, 100)
			return err
		})
	}

	// live == 0 → 建号成功。
	if err := create(owner.ID, "13800000040"); err != nil {
		t.Fatalf("first: %v", err)
	}
	// live == 1 → 第二次建号 409 conflict。
	err := create(owner.ID, "13800000041")
	if err == nil {
		t.Fatal("second create succeeded; want conflict")
	}
	if code := coreerrs.From(err).Code; code != coreerrs.CodeConflict {
		t.Fatalf("second code = %q, want conflict", code)
	}
	if !strings.Contains(err.Error(), "live account") {
		t.Fatalf("second error = %v, want owner live-account wording", err)
	}
	// 另一个属主不受影响。
	if err := create(other.ID, "13800000050"); err != nil {
		t.Fatalf("other owner: %v", err)
	}

	// 宽限期内的软删行不计 live：唯一账号软删后可再建。
	if _, err := e.db.Exec(e.ctx,
		`UPDATE accounts SET deleted_at = now() WHERE phone = '13800000040'`); err != nil {
		t.Fatalf("soft delete: %v", err)
	}
	if err := create(owner.ID, "13800000060"); err != nil {
		t.Fatalf("after grace soft delete: %v", err)
	}

	// 管理员恢复受同一不变量约束：名下有 live 行时零行命中，
	// 按既有零行分类报 capacity_exceeded；live 行软删后恢复成功。
	restoredID := e.scalarString(
		"SELECT id::text FROM accounts WHERE phone = '13800000040'")
	restore := func() error {
		return e.db.InTx(context.Background(), func(ctx context.Context) error {
			_, err := e.accts.Restore(ctx, corescope.Master(), restoredID, 100)
			return err
		})
	}
	if err := restore(); err == nil {
		t.Fatal("restore while another live account exists succeeded")
	} else if code := coreerrs.From(err).Code; code != coreerrs.CodeCapacity {
		t.Fatalf("blocked restore code = %q, want capacity_exceeded", code)
	}
	if _, err := e.db.Exec(e.ctx,
		`UPDATE accounts SET deleted_at = now() WHERE phone = '13800000060'`); err != nil {
		t.Fatalf("soft delete second: %v", err)
	}
	if err := restore(); err != nil {
		t.Fatalf("restore after owner is empty: %v", err)
	}

	// 自助恢复保持既有语义：名下有 live 行 → 400 validation。
	selfOwner := e.createUser("oneacct-self@example.com")
	if err := create(selfOwner.ID, "13800000070"); err != nil {
		t.Fatalf("self owner first: %v", err)
	}
	if _, err := e.db.Exec(e.ctx,
		`UPDATE accounts SET deleted_at = now(), deleted_by = $1::uuid WHERE phone = '13800000070'`,
		selfOwner.ID); err != nil {
		t.Fatalf("self soft delete: %v", err)
	}
	if err := create(selfOwner.ID, "13800000071"); err != nil {
		t.Fatalf("self owner second: %v", err)
	}
	selfDeleted := e.scalarString(
		"SELECT id::text FROM accounts WHERE phone = '13800000070'")
	err = e.db.InTx(context.Background(), func(ctx context.Context) error {
		_, err := e.accts.RestoreSelf(ctx, corescope.User(selfOwner.ID), selfDeleted,
			selfOwner.ID, coreclock.System{}.Now().UTC().Add(-time.Minute), 100)
		return err
	})
	if err == nil {
		t.Fatal("self restore while another live account exists succeeded")
	}
	if code := coreerrs.From(err).Code; code != coreerrs.CodeValidation {
		t.Fatalf("self restore code = %q, want validation", code)
	}
}
