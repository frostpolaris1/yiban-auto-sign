package pg_test

// 每属主账号配额的 e2e：全局上限之外的第二道门。
// 依赖真实 PostgreSQL；计数、插入与 advisory lock 同事务。

import (
	"context"
	"strings"
	"testing"

	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

func TestOwnerAccountQuota(t *testing.T) {
	e := newEnv(t)
	school := e.createSchool("quota")
	owner := e.createUser("quota-owner@example.com").ID
	other := e.createUser("quota-other@example.com").ID

	create := func(ownerID, phone string, ownerLimit int) error {
		return e.db.InTx(context.Background(), func(ctx context.Context) error {
			_, err := e.accts.Create(ctx, coreaccount.New{
				SchoolID: school.ID, OwnerUserID: ownerID, Phone: phone,
			}, 100, ownerLimit)
			return err
		})
	}

	if err := create(owner, "13800055551", 2); err != nil {
		t.Fatalf("first: %v", err)
	}
	if err := create(owner, "13800055552", 2); err != nil {
		t.Fatalf("second: %v", err)
	}
	err := create(owner, "13800055553", 2)
	if err == nil {
		t.Fatal("third create succeeded; want capacity_exceeded")
	}
	if code := coreerrs.From(err).Code; code != coreerrs.CodeCapacity {
		t.Fatalf("third code = %q, want capacity_exceeded", code)
	}
	if err == nil || !strings.Contains(err.Error(), "owner") {
		t.Fatalf("third error = %v, want owner quota message", err)
	}

	// 另一个属主的配额独立。
	if err := create(other, "13800055554", 2); err != nil {
		t.Fatalf("other owner: %v", err)
	}

	// 软删释放名额。
	if _, err := e.db.Exec(e.ctx,
		`UPDATE accounts SET deleted_at = now() WHERE phone = '13800055551'`); err != nil {
		t.Fatalf("soft delete: %v", err)
	}
	if err := create(owner, "13800055555", 2); err != nil {
		t.Fatalf("after soft delete: %v", err)
	}

	// 恢复同样受每属主配额约束：先软删一行，再把名额占满，恢复必须被拒。
	if _, err := e.db.Exec(e.ctx,
		`UPDATE accounts SET deleted_at = now() WHERE phone = '13800055555'`); err != nil {
		t.Fatalf("soft delete 2: %v", err)
	}
	restoreID := e.scalarString(
		"SELECT id::text FROM accounts WHERE phone = '13800055555'")
	restore := func(ownerLimit int) error {
		return e.db.InTx(context.Background(), func(ctx context.Context) error {
			_, err := e.accts.Restore(ctx, corescope.Master(), restoreID, 100, ownerLimit)
			return err
		})
	}
	// 当前活跃：13800055552 与 13800055555（刚软删）；先补满两条。
	if err := create(owner, "13800055556", 2); err != nil {
		t.Fatalf("refill: %v", err)
	}
	if err := restore(2); err == nil {
		t.Fatal("restore over the owner quota succeeded")
	}
	// 释放一行后恢复成功。
	if _, err := e.db.Exec(e.ctx,
		`UPDATE accounts SET deleted_at = now() WHERE phone = '13800055556'`); err != nil {
		t.Fatalf("free a slot: %v", err)
	}
	if err := restore(2); err != nil {
		t.Fatalf("restore after freeing a slot: %v", err)
	}
}
