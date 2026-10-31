package account

// 账号列表用例的守卫单测。
// 判据：零值作用域是调用方缺陷，必须在到达仓储之前拒绝。
// 仓储只接受已定型的范围；把无效范围下传会让权限判断依赖适配器。

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

type fakeListRepo struct {
	Repo
	lists int
	items []Record
}

func (f *fakeListRepo) List(_ context.Context, _ corescope.Scope, _ string, _ Status, _, _ string, _ int, _ bool) ([]Record, string, error) {
	f.lists++
	return f.items, "", nil
}

// failingOwnerReader 模拟属主读库失败：基础设施错误，不是资源缺失。
type failingOwnerReader struct{ OwnerReader }

func (failingOwnerReader) GetByID(context.Context, string) (coreuser.Record, error) {
	return coreuser.Record{}, errors.New("db connection lost")
}

// Create 指定属主时，OwnerReader 的非 not_found 错误必须原样上抛；
// 改写成 404 会把读库故障伪装成「用户不存在」，误导操作者（审查 M4）。
func TestCreateOwnerReaderFailurePassesThrough(t *testing.T) {
	repo := &fakeListRepo{items: []Record{{ID: "acc-1"}}}
	svc, err := NewService(Deps{
		Accounts:   repo,
		Schools:    fakeSchoolReader{},
		Audit:      fakeAuditRepo{},
		Tx:         fakeTxManager{},
		Clock:      clock.Fixed{T: time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)},
		Owner:      failingOwnerReader{},
		Device:     &fakeDeviceReader{},
		Limit:      10,
		OwnerLimit: 10,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	actor := appaudit.Actor{UserID: "boss-1", Role: "master"}
	_, err = svc.Create(context.Background(), corescope.Master(), CreateInput{
		SchoolID:    "school-1",
		Phone:       "13800138000",
		Password:    "secret",
		OwnerUserID: "owner-1",
	}, actor)
	if err == nil {
		t.Fatal("Create succeeded while the owner reader was failing")
	}
	if err.Error() != "db connection lost" {
		t.Fatalf("Create rewrote the reader error: %v", err)
	}
	if strings.Contains(err.Error(), "not found") || strings.Contains(err.Error(), "owner user") {
		t.Fatalf("the failure must not read as not_found: %v", err)
	}
}

// TestListRejectsZeroValueScope 钉住 List 的入口守卫。
// 删掉 RequireScope 时，零值作用域会被下传到仓储——本用例红。
func TestListRejectsZeroValueScope(t *testing.T) {
	repo := &fakeListRepo{items: []Record{{ID: "acc-1"}}}
	svc, err := NewService(Deps{
		Accounts:   repo,
		Schools:    fakeSchoolReader{},
		Audit:      fakeAuditRepo{},
		Tx:         fakeTxManager{},
		Clock:      clock.Fixed{T: time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)},
		Owner:      fakeOwnerReader{},
		Device:     &fakeDeviceReader{},
		Limit:      10,
		OwnerLimit: 10,
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
