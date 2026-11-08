package account

// 异步自动核验的守卫单测（ykxf）。
//
// 判据：开关关闭零外呼；身份一致才 pending→active 并写
// account.auto_approve；不一致、缺绑定、平台故障都留 pending
// 并写 account.auto_check_failed（detail 只含结果词）；
// confirm_defaults 缺失时不启动核验。

import (
	"bytes"
	"context"
	"strings"
	"testing"
	"time"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/school"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

type autoSettings struct {
	enabled bool
	err     error
	calls   int
}

func (f *autoSettings) AutoApproveEnabled(context.Context) (bool, error) {
	f.calls++
	return f.enabled, f.err
}

type autoSchools struct {
	SchoolReader
	record coreschool.Record
	err    error
}

func (f autoSchools) Get(context.Context, string) (coreschool.Record, error) {
	return f.record, f.err
}

type autoRepo struct {
	Repo
	record      Record
	cred        Credential
	statusCalls int
	createCalls int
}

func (r *autoRepo) Get(context.Context, corescope.Scope, string) (Record, error) {
	return r.record, nil
}

func (r *autoRepo) Credential(context.Context, corescope.Scope, string) (Credential, error) {
	return r.cred, nil
}

func (r *autoRepo) SetStatus(_ context.Context, _ corescope.Scope, _ string, from, to Status, _ time.Time, _ string) (Record, error) {
	if r.record.Status != from {
		return Record{}, errs.New(errs.CodeConflict, "status changed")
	}
	r.statusCalls++
	r.record.Status = to
	return r.record, nil
}

func (r *autoRepo) Create(context.Context, New, int) (Record, error) {
	r.createCalls++
	r.record.Status = StatusPending
	return r.record, nil
}

func (r *autoRepo) InsertCredential(context.Context, Credential) error { return nil }

func newAutoService(t *testing.T, repo Repo, schools SchoolReader, settings AutoApproveSettings,
	identity *fakeIdentity) (*Service, *reviewFakeAudit) {
	t.Helper()
	cipher, err := NewCipher(bytes.Repeat([]byte{9}, CredentialKeyBytes))
	if err != nil {
		t.Fatalf("NewCipher: %v", err)
	}
	audits := &reviewFakeAudit{}
	svc, err := NewService(Deps{
		Accounts: repo,
		Schools:  schools,
		Audit:    audits,
		Tx:       reviewFakeTx{},
		Clock:    clock.Fixed{T: time.Date(2026, 11, 8, 6, 0, 0, 0, time.UTC)},
		Cipher:   cipher,
		Owner:    fakeOwnerReader{},
		Device:   &fakeDeviceReader{},
		Settings: settings,
		Identity: identity,
		Limit:    10,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc, audits
}

func sealedCredential(t *testing.T, cipher *Cipher, accountID string) Credential {
	t.Helper()
	cred, err := cipher.Seal("school-1", accountID, CredentialPayload{Password: "stored-secret"})
	if err != nil {
		t.Fatalf("Seal: %v", err)
	}
	return cred
}

func auditActionCount(audits *reviewFakeAudit, action string) int {
	count := 0
	for _, entry := range audits.entries {
		if string(entry.Action) == action {
			count++
		}
	}
	return count
}

func TestRunAutoApproveMatchesAndActivates(t *testing.T) {
	repo := &autoRepo{record: Record{ID: "acc-1", SchoolID: "school-1", Status: StatusPending}}
	svc, audits := newAutoService(t, repo,
		autoSchools{record: coreschool.Record{ID: "school-1", Status: coreschool.StatusActive, PlatformUniversityID: "U1"}},
		&autoSettings{enabled: true},
		&fakeIdentity{profile: contract.Profile{UniversityID: "U1"}})
	svc.deps.Cipher = autoCipher(t)
	repo.cred = sealedCredential(t, svc.deps.Cipher, "acc-1")

	svc.runAutoApprove(context.Background(), "acc-1", "school-1")

	if repo.record.Status != StatusActive {
		t.Fatalf("status = %q, want active", repo.record.Status)
	}
	if count := auditActionCount(audits, "account.auto_approve"); count != 1 {
		t.Fatalf("auto approve audit rows = %d, want 1", count)
	}
	for _, entry := range audits.entries {
		if string(entry.Action) == "account.auto_approve" {
			if entry.ActorLabel != "auto-approve" || entry.ActorUserID != "" || entry.ActorRole != "" {
				t.Fatalf("actor = %+v", entry)
			}
			if entry.Detail["from"] != "pending" || entry.Detail["to"] != "active" {
				t.Fatalf("detail = %#v", entry.Detail)
			}
		}
	}
}

func TestRunAutoApproveMismatchStaysPending(t *testing.T) {
	repo := &autoRepo{record: Record{ID: "acc-2", SchoolID: "school-1", Status: StatusPending}}
	svc, audits := newAutoService(t, repo,
		autoSchools{record: coreschool.Record{ID: "school-1", Status: coreschool.StatusActive, PlatformUniversityID: "U1"}},
		&autoSettings{enabled: true},
		&fakeIdentity{profile: contract.Profile{UniversityID: "U2"}})
	svc.deps.Cipher = autoCipher(t)
	repo.cred = sealedCredential(t, svc.deps.Cipher, "acc-2")

	svc.runAutoApprove(context.Background(), "acc-2", "school-1")

	if repo.record.Status != StatusPending {
		t.Fatalf("status = %q, want pending", repo.record.Status)
	}
	if count := auditActionCount(audits, "account.auto_check_failed"); count != 1 {
		t.Fatalf("failed audit rows = %d, want 1", count)
	}
	for _, entry := range audits.entries {
		if string(entry.Action) == "account.auto_check_failed" {
			if len(entry.Detail) != 1 || entry.Detail["result"] != "identity_mismatch" {
				t.Fatalf("detail = %#v, want only result=identity_mismatch", entry.Detail)
			}
			if strings.Contains(entry.Detail["result"].(string), "U2") {
				t.Fatalf("detail leaked the platform value: %#v", entry.Detail)
			}
		}
	}
}

func TestRunAutoApproveSwitchOffDoesNothing(t *testing.T) {
	repo := &autoRepo{record: Record{ID: "acc-3", SchoolID: "school-1", Status: StatusPending}}
	identity := &fakeIdentity{}
	svc, audits := newAutoService(t, repo, autoSchools{}, &autoSettings{enabled: false}, identity)
	svc.deps.Cipher = autoCipher(t)
	repo.cred = sealedCredential(t, svc.deps.Cipher, "acc-3")

	svc.runAutoApprove(context.Background(), "acc-3", "school-1")

	if identity.calls != 0 {
		t.Fatalf("platform calls = %d, want 0", identity.calls)
	}
	if len(audits.entries) != 0 {
		t.Fatalf("audit rows = %d, want 0", len(audits.entries))
	}
	if repo.record.Status != StatusPending {
		t.Fatalf("status = %q, want pending", repo.record.Status)
	}
}

func TestRunAutoApprovePlatformFailureAuditsResult(t *testing.T) {
	repo := &autoRepo{record: Record{ID: "acc-4", SchoolID: "school-1", Status: StatusPending}}
	svc, audits := newAutoService(t, repo,
		autoSchools{record: coreschool.Record{ID: "school-1", Status: coreschool.StatusActive, PlatformUniversityID: "U1"}},
		&autoSettings{enabled: true},
		&fakeIdentity{err: errs.New(errs.CodeUpstream, "network down")})
	svc.deps.Cipher = autoCipher(t)
	repo.cred = sealedCredential(t, svc.deps.Cipher, "acc-4")

	svc.runAutoApprove(context.Background(), "acc-4", "school-1")

	if repo.record.Status != StatusPending {
		t.Fatalf("status = %q, want pending", repo.record.Status)
	}
	for _, entry := range audits.entries {
		if string(entry.Action) == "account.auto_check_failed" {
			if entry.Detail["result"] != "platform_error" {
				t.Fatalf("result = %v, want platform_error", entry.Detail["result"])
			}
		}
	}
}

func TestCreateWithConfirmDefaultsSchedulesCheck(t *testing.T) {
	repo := &autoRepo{record: Record{ID: "acc-5", SchoolID: "school-1", Status: StatusPending}}
	identity := &fakeIdentity{profile: contract.Profile{UniversityID: "U1"}}
	svc, audits := newAutoService(t, repo,
		autoSchools{record: coreschool.Record{ID: "school-1", Status: coreschool.StatusActive, PlatformUniversityID: "U1"}},
		&autoSettings{enabled: true}, identity)
	repo.cred = sealedCredential(t, svc.deps.Cipher, "acc-5")

	_, err := svc.Create(context.Background(), corescope.Master(), CreateInput{
		SchoolID: "school-1", Phone: "13800138051", Password: "secret", ConfirmDefaults: true,
	}, fakeActor())
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	svc.waitBackground()

	if identity.calls != 1 {
		t.Fatalf("platform calls = %d, want 1", identity.calls)
	}
	if repo.record.Status != StatusActive {
		t.Fatalf("status = %q, want active", repo.record.Status)
	}
	if count := auditActionCount(audits, "account.auto_approve"); count != 1 {
		t.Fatalf("auto approve audit rows = %d, want 1", count)
	}
}

func TestCreateWithoutConfirmDefaultsSkipsCheck(t *testing.T) {
	repo := &autoRepo{record: Record{ID: "acc-6", SchoolID: "school-1", Status: StatusPending}}
	identity := &fakeIdentity{profile: contract.Profile{UniversityID: "U1"}}
	svc, audits := newAutoService(t, repo,
		autoSchools{record: coreschool.Record{ID: "school-1", Status: coreschool.StatusActive, PlatformUniversityID: "U1"}},
		&autoSettings{enabled: true}, identity)
	repo.cred = sealedCredential(t, svc.deps.Cipher, "acc-6")

	_, err := svc.Create(context.Background(), corescope.Master(), CreateInput{
		SchoolID: "school-1", Phone: "13800138052", Password: "secret",
	}, fakeActor())
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	svc.waitBackground()

	if identity.calls != 0 {
		t.Fatalf("platform calls = %d, want 0", identity.calls)
	}
	if repo.record.Status != StatusPending {
		t.Fatalf("status = %q, want pending", repo.record.Status)
	}
	if len(audits.entries) != 1 {
		t.Fatalf("audit rows = %d, want only account.create", len(audits.entries))
	}
}

func autoCipher(t *testing.T) *Cipher {
	t.Helper()
	cipher, err := NewCipher(bytes.Repeat([]byte{11}, CredentialKeyBytes))
	if err != nil {
		t.Fatalf("NewCipher: %v", err)
	}
	return cipher
}

func fakeActor() (actor appaudit.Actor) {
	return appaudit.Actor{UserID: "master-1", Role: "master", Label: "m***@example.com"}
}
