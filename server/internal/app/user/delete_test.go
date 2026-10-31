package user

// Delete 的最后 active master 判据单测（独立审查 P2-3）。
//
// # 功能
// 钉住两件事：Delete 在目标为 master 时必须参与 master 计数；
// 计数为零即 409，行不删、审计零行。
// HTTP 面做不到「已停用/已删的 master actor」：会话解析层先拒绝。
// 故 scope 由本文件伪造，直接调 Service.Delete。
//
// # 归属
// app 层单测。锁串行化的承重性质在 adapters/pg（UserCapacityLock），
// 本文件无法验证锁本身，只验证用例调用了计数并服从其结果。
//
// # 口径
// 计数谓词是 role='master' AND status='active' AND deleted_at IS NULL
// （独立审查 P1-2 修订：停用 master 不能登录，不构成恢复路径）。

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// appauditActor 构造审计主体。
func appauditActor(id string) audit.Actor {
	return audit.Actor{UserID: id, Role: coreauth.RoleMaster}
}

// fakeUserRepo 是内存版用户仓储。
// CountLiveMasters 的 active-only 谓词在 fake 里复算一遍：
// 用例若改判据而适配器未同步，两侧行为差异会让本文件可见。
type fakeUserRepo struct {
	records     map[string]coreuser.Record
	softDeleted []string
	roleUpdated []string
}

func newFakeUserRepo(records ...coreuser.Record) *fakeUserRepo {
	repo := &fakeUserRepo{records: map[string]coreuser.Record{}}
	for _, rec := range records {
		repo.records[rec.ID] = rec
	}
	return repo
}

func (f *fakeUserRepo) Create(context.Context, New, int) (Record, error) {
	return Record{}, errors.New("not used")
}

func (f *fakeUserRepo) GetByID(_ context.Context, id string) (Record, error) {
	if rec, ok := f.records[id]; ok && rec.DeletedAt == nil {
		return rec, nil
	}
	return Record{}, errs.ErrNotFound
}

func (f *fakeUserRepo) GetByEmail(context.Context, string) (Record, error) {
	return Record{}, errors.New("not used")
}

func (f *fakeUserRepo) List(context.Context, coreauth.Role, coreauth.Status, string, string, int, bool) ([]Record, string, error) {
	return nil, "", nil
}

func (f *fakeUserRepo) UpdatePassword(context.Context, string, string, bool, time.Time) error {
	return errors.New("not used")
}

func (f *fakeUserRepo) RehashPassword(context.Context, string, string) error {
	return errors.New("not used")
}

func (f *fakeUserRepo) UpdateRole(_ context.Context, id string, role coreauth.Role, _ time.Time) error {
	rec, ok := f.records[id]
	if !ok || rec.DeletedAt != nil {
		return errs.ErrNotFound
	}
	rec.Role = role
	f.records[id] = rec
	f.roleUpdated = append(f.roleUpdated, id)
	return nil
}

func (f *fakeUserRepo) SetStatus(context.Context, string, coreauth.Status, time.Time) error {
	return errors.New("not used")
}

func (f *fakeUserRepo) MarkLogin(context.Context, string, time.Time) error {
	return errors.New("not used")
}

func (f *fakeUserRepo) SoftDelete(_ context.Context, id, _ string, at time.Time) error {
	rec, ok := f.records[id]
	if !ok || rec.DeletedAt != nil {
		return errs.ErrNotFound
	}
	rec.DeletedAt = &at
	f.records[id] = rec
	f.softDeleted = append(f.softDeleted, id)
	return nil
}

func (f *fakeUserRepo) Restore(context.Context, string, int) (Record, error) {
	return Record{}, errors.New("not used")
}

// CountLiveMasters 按判据 role='master' AND status='active' AND deleted_at IS NULL
// 复算，排除 excludeID。
func (f *fakeUserRepo) CountLiveMasters(_ context.Context, excludeID string) (int, error) {
	n := 0
	for id, rec := range f.records {
		if id == excludeID {
			continue
		}
		if rec.Role == coreauth.RoleMaster && rec.Status == coreauth.StatusActive && rec.DeletedAt == nil {
			n++
		}
	}
	return n, nil
}

func (f *fakeUserRepo) Lookup(context.Context, string, int, bool) ([]Record, error) {
	return nil, nil
}

type fakeCounter struct{ n int }

func (f fakeCounter) CountLiveAccountsByOwner(context.Context, string) (int, error) {
	return f.n, nil
}

type fakeAudit struct {
	entries []audit.Entry
}

func (f *fakeAudit) Append(_ context.Context, in audit.Entry) (audit.Row, error) {
	f.entries = append(f.entries, in)
	return audit.Row{}, nil
}

func (f *fakeAudit) Verify(context.Context, int64, int64) (audit.Report, error) {
	return audit.Report{}, nil
}

type fakeTx struct{}

func (fakeTx) InTx(ctx context.Context, fn func(ctx context.Context) error) error { return fn(ctx) }

type fakeIssuer struct{}

func (fakeIssuer) Issue(context.Context, coreauth.User) (coreauth.Issued, error) {
	return coreauth.Issued{}, errors.New("not used")
}

func (fakeIssuer) Resolve(context.Context, string) (coreauth.Session, error) {
	return coreauth.Session{}, coreauth.ErrNoSession
}

func (fakeIssuer) Revoke(context.Context, string) error { return nil }

func (fakeIssuer) RevokeAllForUser(context.Context, string) error { return nil }

func newTestService(repo *fakeUserRepo, audits *fakeAudit) (*Service, error) {
	return NewService(Deps{
		Users:         repo,
		Audit:         audits,
		Tx:            fakeTx{},
		Clock:         clock.System{},
		KDF:           coreauth.PlainHasher{},
		Limit:         10000,
		Sessions:      fakeIssuer{},
		OwnerAccounts: fakeCounter{},
	})
}

// TestDeleteBlocksLastActiveMaster：唯一 active master 不可被 Delete。
// actor 是已停用的 master（HTTP 面不可达，scope 伪造）——
// 修好前该调用把系统推向零 active master。
func TestDeleteBlocksLastActiveMaster(t *testing.T) {
	disabled := Record{ID: "00000000-0000-4000-8000-00000000000a", Role: coreauth.RoleMaster, Status: coreauth.StatusDisabled}
	target := Record{ID: "00000000-0000-4000-8000-00000000000b", Role: coreauth.RoleMaster, Status: coreauth.StatusActive}
	repo := newFakeUserRepo(disabled, target)
	audits := &fakeAudit{}
	svc, err := newTestService(repo, audits)
	if err != nil {
		t.Fatalf("new service: %v", err)
	}

	err = svc.Delete(context.Background(), corescope.Master(), target.ID,
		appauditActor("00000000-0000-4000-8000-00000000000a"))
	if err == nil {
		t.Fatal("deleting the last active master succeeded")
	}
	var coded *errs.Error
	if !errors.As(err, &coded) || coded.Code != errs.CodeConflict {
		t.Fatalf("err = %v, want conflict", err)
	}
	if len(repo.softDeleted) != 0 {
		t.Fatalf("soft-deleted = %v, want none", repo.softDeleted)
	}
	if len(audits.entries) != 0 {
		t.Fatalf("audit rows = %d, want 0", len(audits.entries))
	}
}

// TestDeleteAllowsWhenAnotherActiveMasterRemains：两名 active master 删其一放行。
func TestDeleteAllowsWhenAnotherActiveMasterRemains(t *testing.T) {
	first := Record{ID: "00000000-0000-4000-8000-00000000000c", Role: coreauth.RoleMaster, Status: coreauth.StatusActive}
	second := Record{ID: "00000000-0000-4000-8000-00000000000d", Role: coreauth.RoleMaster, Status: coreauth.StatusActive}
	repo := newFakeUserRepo(first, second)
	svc, err := newTestService(repo, &fakeAudit{})
	if err != nil {
		t.Fatalf("new service: %v", err)
	}

	err = svc.Delete(context.Background(), corescope.Master(), second.ID,
		appauditActor(first.ID))
	if err != nil {
		t.Fatalf("delete with a remaining active master: %v", err)
	}
	if len(repo.softDeleted) != 1 || repo.softDeleted[0] != second.ID {
		t.Fatalf("soft-deleted = %v, want exactly %s", repo.softDeleted, second.ID)
	}
}
