package account

// 账号列表用例的守卫单测。
// 判据：零值作用域是调用方缺陷，必须在到达仓储之前拒绝。
// 仓储只接受已定型的范围；把无效范围下传会让权限判断依赖适配器。

import (
	"context"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

type fakeListRepo struct {
	Repo
	lists int
	items []Record
}

func (f *fakeListRepo) List(_ context.Context, _ corescope.Scope, _ string, _ Status, _, _ string, _ int) ([]Record, string, error) {
	f.lists++
	return f.items, "", nil
}

// TestListRejectsZeroValueScope 钉住 List 的入口守卫。
// 删掉 RequireScope 时，零值作用域会被下传到仓储——本用例红。
func TestListRejectsZeroValueScope(t *testing.T) {
	repo := &fakeListRepo{items: []Record{{ID: "acc-1"}}}
	svc, err := NewService(Deps{
		Accounts: repo,
		Schools:  fakeSchoolReader{},
		Audit:    fakeAuditRepo{},
		Tx:       fakeTxManager{},
		Clock:    clock.Fixed{T: time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)},
		Limit:    10,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}

	if _, err := svc.List(context.Background(), corescope.Scope{}, ListFilter{}, "", 10); err == nil {
		t.Fatal("List accepted a zero-value scope")
	} else if got := errs.From(err).Code; got != errs.CodeInternal {
		t.Fatalf("zero-value scope maps to %q; want internal_error", got)
	}
	if repo.lists != 0 {
		t.Fatalf("List queried the repository %d times with a zero-value scope", repo.lists)
	}
}
