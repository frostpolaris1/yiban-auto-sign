package account

// requireSchool 的守卫单测。
// 判据：建号只落 active 学校。disabled 行存在但停止新工作，
// 与「查无此校」必须可分辨；实现里只此一处，建号是唯一调用点。

import (
	"context"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/school"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// statusSchoolReader 返回固定状态的院校行，供 requireSchool 分支断言。
type statusSchoolReader struct {
	SchoolReader
	status coreschool.Status
}

func (f statusSchoolReader) Get(context.Context, string) (coreschool.Record, error) {
	return coreschool.Record{ID: "school-1", Status: f.status}, nil
}

func newSchoolGuardService(t *testing.T, reader SchoolReader) *Service {
	t.Helper()
	svc, err := NewService(Deps{
		Accounts: &fakeListRepo{},
		Schools:  reader,
		Audit:    fakeAuditRepo{},
		Tx:       fakeTxManager{},
		Clock:    clock.Fixed{T: time.Date(2026, 11, 8, 6, 0, 0, 0, time.UTC)},
		Cipher:   nil,
		Owner:    fakeOwnerReader{},
		Device:   &fakeDeviceReader{},
		Limit:    10,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

// disabled 学校拒绝建号：冲突语义——行存在，是状态不允许，不是查无此校。
// user 与 admin/master 走同一个分支，一并钉住。
func TestRequireSchoolRejectsDisabled(t *testing.T) {
	svc := newSchoolGuardService(t, statusSchoolReader{status: coreschool.StatusDisabled})
	for _, scope := range []corescope.Scope{
		corescope.Master(),
		corescope.Admin([]string{"school-1"}),
		corescope.User("u1"),
	} {
		err := svc.requireSchool(context.Background(), scope, "school-1")
		if err == nil {
			t.Fatalf("%s: disabled school passed requireSchool", string(scope.Kind()))
		}
		if got := errs.From(err).Code; got != errs.CodeConflict {
			t.Fatalf("%s: code = %q, want conflict", string(scope.Kind()), got)
		}
	}
}

// active 学校放行：既有口径不回退。
func TestRequireSchoolAcceptsActive(t *testing.T) {
	svc := newSchoolGuardService(t, statusSchoolReader{status: coreschool.StatusActive})
	if err := svc.requireSchool(context.Background(), corescope.Master(), "school-1"); err != nil {
		t.Fatalf("active school rejected: %v", err)
	}
}
