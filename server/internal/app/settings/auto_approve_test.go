package settings_test

// 自动审批开关的守卫单测（ykxf）。
//
// 判据：读 master/admin 放行、写只许 master；写入与
// settings.auto_approve 审计同事务；detail 只含字段与目标状态；
// 未装配开关仓储时失败关闭。

import (
	"context"
	"testing"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appsettings "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/settings"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

type fakeAutoApprove struct {
	enabled bool
	writes  []bool
	by      string
	err     error
}

func (f *fakeAutoApprove) AutoApproveEnabled(context.Context) (bool, error) {
	return f.enabled, f.err
}

func (f *fakeAutoApprove) SetAutoApproveEnabled(_ context.Context, enabled bool, by string) error {
	if f.err != nil {
		return f.err
	}
	f.writes = append(f.writes, enabled)
	f.by = by
	f.enabled = enabled
	return nil
}

func newAutoApproveService(t *testing.T, auto appsettings.AutoApproveStore) (*appsettings.Service, *fakeAudit) {
	t.Helper()
	audits := &fakeAudit{}
	svc, err := appsettings.NewService(appsettings.Deps{
		Store: &fakeStore{}, Audit: audits, Tx: &fakeTx{}, Clock: fixedClock{},
		AutoApprove: auto,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc, audits
}

func TestAutoApproveReadAllowsMasterAndAdmin(t *testing.T) {
	auto := &fakeAutoApprove{enabled: true}
	svc, audits := newAutoApproveService(t, auto)

	for _, scope := range []corescope.Scope{corescope.Master(), corescope.Admin([]string{"school-1"})} {
		enabled, err := svc.AutoApprove(context.Background(), scope)
		if err != nil || !enabled {
			t.Fatalf("AutoApprove(%v) = %v, %v", scope.Kind(), enabled, err)
		}
	}
	if _, err := svc.AutoApprove(context.Background(), corescope.User("user-1")); err == nil ||
		errs.From(err).Code != errs.CodeForbidden {
		t.Fatalf("user read error = %v, want forbidden", err)
	}
	if len(audits.entries) != 0 {
		t.Fatalf("read wrote %d audit rows, want 0", len(audits.entries))
	}
}

func TestAutoApproveWriteIsMasterOnlyAndAudits(t *testing.T) {
	auto := &fakeAutoApprove{}
	svc, audits := newAutoApproveService(t, auto)
	actor := appaudit.Actor{UserID: "master-1", Role: coreauth.RoleMaster, Label: "m***@example.com"}

	if _, err := svc.SetAutoApprove(context.Background(), corescope.Admin([]string{"school-1"}), actor, true); err == nil ||
		errs.From(err).Code != errs.CodeForbidden {
		t.Fatalf("admin write error = %v, want forbidden", err)
	}
	if len(auto.writes) != 0 {
		t.Fatalf("a forbidden write reached the store: %v", auto.writes)
	}

	enabled, err := svc.SetAutoApprove(context.Background(), corescope.Master(), actor, true)
	if err != nil || !enabled {
		t.Fatalf("SetAutoApprove = %v, %v", enabled, err)
	}
	if len(auto.writes) != 1 || !auto.writes[0] || auto.by != "master-1" {
		t.Fatalf("store writes = %v by %q", auto.writes, auto.by)
	}
	if len(audits.entries) != 1 {
		t.Fatalf("audit rows = %d, want 1", len(audits.entries))
	}
	entry := audits.entries[0]
	if entry.Action != coreaudit.ActionSettingsAutoApprove {
		t.Fatalf("action = %q, want settings.auto_approve", entry.Action)
	}
	if entry.TargetType != "settings" || entry.TargetID != "auto_approve" {
		t.Fatalf("target = %s/%s", entry.TargetType, entry.TargetID)
	}
	if len(entry.Detail) != 2 || entry.Detail["field"] != "auto_approve" || entry.Detail["to"] != "enabled" {
		t.Fatalf("detail = %#v", entry.Detail)
	}
}

func TestAutoApproveFailsClosedWithoutStore(t *testing.T) {
	svc, _ := newAutoApproveService(t, nil)
	if _, err := svc.AutoApprove(context.Background(), corescope.Master()); err == nil ||
		errs.From(err).Code != errs.CodeInternal {
		t.Fatalf("missing store read error = %v, want internal", err)
	}
	actor := appaudit.Actor{UserID: "master-1", Role: coreauth.RoleMaster}
	if _, err := svc.SetAutoApprove(context.Background(), corescope.Master(), actor, true); err == nil ||
		errs.From(err).Code != errs.CodeInternal {
		t.Fatalf("missing store write error = %v, want internal", err)
	}
}
