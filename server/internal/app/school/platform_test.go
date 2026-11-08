package school

// 平台大学投影的守卫单测（ykxf）。
//
// 判据：活跃命中复用、disabled 视同未命中且不重复建校、
// 未命中自动建校并只写 university_id 审计、并发建校 409 回落复用。
// 本文件不连数据库：仓储、审计与事务都是内存替身。

import (
	"context"
	"regexp"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

type platformRepo struct {
	Repo
	lookup    Record
	lookupErr error
	lookups   int
	creates   []New
	createErr error
	created   Record
	// afterConflict 在 Create 返回冲突后切换查询结果，模拟并发赢家已提交。
	afterConflict func()
}

func (r *platformRepo) GetByPlatformUniversityID(context.Context, string) (Record, error) {
	r.lookups++
	return r.lookup, r.lookupErr
}

func (r *platformRepo) Create(_ context.Context, in New) (Record, error) {
	r.creates = append(r.creates, in)
	if r.createErr != nil {
		if r.afterConflict != nil {
			r.afterConflict()
		}
		return Record{}, r.createErr
	}
	if r.created.ID != "" {
		return r.created, nil
	}
	return Record{
		ID:                   "school-auto",
		Code:                 in.Code,
		Name:                 in.Name,
		Status:               StatusActive,
		PlatformUniversityID: in.PlatformUniversityID,
		CreatedAt:            time.Date(2026, 11, 8, 0, 0, 0, 0, time.UTC),
		UpdatedAt:            time.Date(2026, 11, 8, 0, 0, 0, 0, time.UTC),
	}, nil
}

type platformTx struct{ app.TxManager }

func (platformTx) InTx(ctx context.Context, fn func(context.Context) error) error { return fn(ctx) }

type platformAudit struct {
	appaudit.Repo
	entries []coreaudit.Entry
}

func (f *platformAudit) Append(_ context.Context, in coreaudit.Entry) (coreaudit.Row, error) {
	f.entries = append(f.entries, in)
	return coreaudit.Row{}, nil
}

func newPlatformService(t *testing.T, repo Repo) (*Service, *platformAudit) {
	t.Helper()
	audits := &platformAudit{}
	svc, err := NewService(Deps{
		Schools: repo,
		Audit:   audits,
		Tx:      platformTx{},
		Clock:   clock.Fixed{T: time.Date(2026, 11, 8, 6, 0, 0, 0, time.UTC)},
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc, audits
}

var autoCodePattern = regexp.MustCompile(`^auto-[0-9a-f]{12}$`)

func TestEnsurePlatformSchoolReusesActiveBinding(t *testing.T) {
	repo := &platformRepo{lookup: Record{
		ID: "school-1", Code: "existing", Name: "Existing",
		Status: StatusActive, PlatformUniversityID: "0000000001",
	}}
	svc, audits := newPlatformService(t, repo)

	record, err := svc.EnsurePlatformSchool(context.Background(), "0000000001", "示例大学")
	if err != nil {
		t.Fatalf("EnsurePlatformSchool: %v", err)
	}
	if record.ID != "school-1" || record.Status != StatusActive {
		t.Fatalf("record = %+v", record)
	}
	if len(repo.creates) != 0 {
		t.Fatalf("active hit created %d schools, want 0", len(repo.creates))
	}
	if len(audits.entries) != 0 {
		t.Fatalf("active hit wrote %d audit rows, want 0", len(audits.entries))
	}
}

func TestEnsurePlatformSchoolTreatsDisabledAsAbsent(t *testing.T) {
	repo := &platformRepo{lookup: Record{
		ID: "school-off", Code: "off", Name: "Off",
		Status: StatusDisabled, PlatformUniversityID: "0000000002",
	}}
	svc, audits := newPlatformService(t, repo)

	record, err := svc.EnsurePlatformSchool(context.Background(), "0000000002", "停用大学")
	if err != nil {
		t.Fatalf("EnsurePlatformSchool: %v", err)
	}
	if record.Status != StatusDisabled || record.ID != "school-off" {
		t.Fatalf("record = %+v", record)
	}
	if len(repo.creates) != 0 {
		t.Fatalf("disabled hit created %d schools, want 0", len(repo.creates))
	}
	if len(audits.entries) != 0 {
		t.Fatalf("disabled hit wrote %d audit rows, want 0", len(audits.entries))
	}
}

func TestEnsurePlatformSchoolCreatesAndAuditsOnlyUniversityID(t *testing.T) {
	repo := &platformRepo{lookupErr: errs.ErrNotFound}
	svc, audits := newPlatformService(t, repo)

	record, err := svc.EnsurePlatformSchool(context.Background(), "0000000003", "  示例大学  ")
	if err != nil {
		t.Fatalf("EnsurePlatformSchool: %v", err)
	}
	if record.Status != StatusActive || record.PlatformUniversityID != "0000000003" {
		t.Fatalf("created record = %+v", record)
	}
	if record.Name != "示例大学" {
		t.Fatalf("created name = %q, want the trimmed platform name", record.Name)
	}
	if !autoCodePattern.MatchString(record.Code) {
		t.Fatalf("generated code = %q, want auto-<12 hex>", record.Code)
	}
	if len(repo.creates) != 1 {
		t.Fatalf("create calls = %d, want 1", len(repo.creates))
	}
	if len(audits.entries) != 1 {
		t.Fatalf("audit rows = %d, want 1", len(audits.entries))
	}
	entry := audits.entries[0]
	if entry.Action != coreaudit.ActionSchoolAutoCreate {
		t.Fatalf("action = %q, want school.auto_create", entry.Action)
	}
	if entry.ActorUserID != "" || entry.ActorRole != "" || entry.ActorLabel != "auto-detect" {
		t.Fatalf("actor = %+v, want the system actor", entry)
	}
	if len(entry.Detail) != 1 || entry.Detail["university_id"] != "0000000003" {
		t.Fatalf("detail = %#v, want only university_id", entry.Detail)
	}
}

func TestEnsurePlatformSchoolFallsBackToReuseOnConcurrentConflict(t *testing.T) {
	repo := &platformRepo{
		lookupErr: errs.ErrNotFound,
		createErr: errs.New(errs.CodeConflict, "conflict"),
	}
	repo.afterConflict = func() {
		repo.lookup = Record{
			ID: "school-winner", Code: "auto-000000000004", Name: "Winner",
			Status: StatusActive, PlatformUniversityID: "0000000004",
		}
		repo.lookupErr = nil
	}
	svc, audits := newPlatformService(t, repo)

	record, err := svc.EnsurePlatformSchool(context.Background(), "0000000004", "并发大学")
	if err != nil {
		t.Fatalf("EnsurePlatformSchool: %v", err)
	}
	if record.ID != "school-winner" {
		t.Fatalf("record = %+v, want the concurrent winner", record)
	}
	if repo.lookups != 2 {
		t.Fatalf("lookups = %d, want 2", repo.lookups)
	}
	if len(audits.entries) != 0 {
		t.Fatalf("a failed insert wrote %d audit rows, want 0", len(audits.entries))
	}
}

func TestEnsurePlatformSchoolRejectsEmptyName(t *testing.T) {
	repo := &platformRepo{lookupErr: errs.ErrNotFound}
	svc, _ := newPlatformService(t, repo)

	_, err := svc.EnsurePlatformSchool(context.Background(), "0000000005", "   ")
	if err == nil || errs.From(err).Code != errs.CodeValidation {
		t.Fatalf("empty name error = %v, want validation", err)
	}
	if len(repo.creates) != 0 {
		t.Fatalf("empty name created %d schools, want 0", len(repo.creates))
	}
}
