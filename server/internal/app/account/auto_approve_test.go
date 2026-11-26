package account

// 异步自动核验的守卫单测（ykxf）。
//
// 判据：开关关闭零外呼；身份一致才 pending→active 并写
// account.auto_approve；不一致、缺绑定、平台故障都留 pending
// 并写 account.auto_check_failed（detail 只含结果词）；
// confirm_defaults 缺失时不启动核验；
// P3-14：confirm_defaults+开关开 的建号只发**最终态**一条通知
// （成功＝auto_approved 文案，失败＝pending 文案），不再
// pending 与 auto_approved 各发一份。

import (
	"bytes"
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
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
	record             Record
	cred               Credential
	statusCalls        int
	versionStatusCalls int
	createCalls        int
	getCalls           int
	// flipAfter > 0 时，第 flipAfter 次之后的 Get 返回 flipRecord：
	// 模拟两次读之间人工已把行改成终态。
	flipAfter  int
	flipRecord Record
}

func (r *autoRepo) Get(context.Context, corescope.Scope, string) (Record, error) {
	r.getCalls++
	if r.flipAfter > 0 && r.getCalls > r.flipAfter {
		return r.flipRecord, nil
	}
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

// SetStatusIfCredentialVersion 是自动审批激活用的版本 CAS 替身：
// 状态与凭据版本都匹配才迁移，否则冲突。旧实现在核验后只做状态 CAS。
func (r *autoRepo) SetStatusIfCredentialVersion(_ context.Context, _ corescope.Scope, _ string, from, to Status, expectedVersion int32, _ time.Time, _ string) (Record, error) {
	r.versionStatusCalls++
	if r.record.Status != from || r.cred.Version != expectedVersion {
		return Record{}, errs.New(errs.CodeConflict, "status or credential version changed")
	}
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
		Quota:    fixedQuota(10),
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

	svc.runAutoApprove(context.Background(), "acc-1", "school-1", false)

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

	svc.runAutoApprove(context.Background(), "acc-2", "school-1", false)

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

	svc.runAutoApprove(context.Background(), "acc-3", "school-1", false)

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

	svc.runAutoApprove(context.Background(), "acc-4", "school-1", false)

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

// TestRunAutoApproveCredentialVersionChangedStaysPending 钉住读凭据→核验→激活
// 之间的凭据版本 CAS：核验期间凭据被替换（version+1）时不得激活账号。
// 账号必须留 pending，只写 account.auto_check_failed（result=credential_changed），
// 不写 account.auto_approve。旧实现激活时只做状态 CAS，本用例红。
func TestRunAutoApproveCredentialVersionChangedStaysPending(t *testing.T) {
	repo := &autoRepo{record: Record{ID: "acc-v1", SchoolID: "school-1", Status: StatusPending}}
	identity := &fakeIdentity{profile: contract.Profile{UniversityID: "U1"}}
	svc, audits := newAutoService(t, repo,
		autoSchools{record: coreschool.Record{ID: "school-1", Status: coreschool.StatusActive, PlatformUniversityID: "U1"}},
		&autoSettings{enabled: true}, identity)
	repo.cred = sealedCredential(t, svc.deps.Cipher, "acc-v1")
	if repo.cred.Version != 1 {
		t.Fatalf("sealed credential version = %d, want 1", repo.cred.Version)
	}
	// 平台核验期间凭据被替换：用例读到的版本已经过期。
	identity.onVerify = func() { repo.cred.Version++ }

	svc.runAutoApprove(context.Background(), "acc-v1", "school-1", false)

	if repo.record.Status != StatusPending {
		t.Fatalf("status = %q, want pending（版本已变不得激活）", repo.record.Status)
	}
	if repo.statusCalls != 0 {
		t.Fatalf("status-only CAS calls = %d, want 0", repo.statusCalls)
	}
	if repo.versionStatusCalls != 1 {
		t.Fatalf("version CAS attempts = %d, want 1", repo.versionStatusCalls)
	}
	if count := auditActionCount(audits, "account.auto_approve"); count != 0 {
		t.Fatalf("auto approve audit rows = %d, want 0", count)
	}
	if count := auditActionCount(audits, "account.auto_check_failed"); count != 1 {
		t.Fatalf("auto check failed rows = %d, want 1", count)
	}
	for _, entry := range audits.entries {
		if string(entry.Action) == "account.auto_check_failed" {
			if len(entry.Detail) != 1 || entry.Detail["result"] != "credential_changed" {
				t.Fatalf("detail = %#v, want only result=credential_changed", entry.Detail)
			}
		}
	}
}

// TestRunAutoApproveVersionConflictAfterManualReviewStaysSilent 是否定手臂：
// 版本冲突的判定读到 pending，但 recordAutoCheckFailed 内部的复核发现人工
// 已通过——两类审计都不写，也不得补发待审通知（N1：通知只跟审计走）。
// flipAfter=2 让第 3 次 Get（复核那次）返回 active：第 1 次是
// SigningCredential，第 2 次是冲突判定。
func TestRunAutoApproveVersionConflictAfterManualReviewStaysSilent(t *testing.T) {
	repo := &autoRepo{
		record:     Record{ID: "acc-v2", SchoolID: "school-1", Status: StatusPending},
		flipAfter:  2,
		flipRecord: Record{ID: "acc-v2", SchoolID: "school-1", Status: StatusActive},
	}
	identity := &fakeIdentity{profile: contract.Profile{UniversityID: "U1"}}
	alerts := &recordingReviewAlerts{}
	svc := newAutoServiceAlerts(t, repo,
		autoSchools{record: coreschool.Record{ID: "school-1", Status: coreschool.StatusActive, PlatformUniversityID: "U1"}},
		&autoSettings{enabled: true}, identity, alerts)
	repo.cred = sealedCredential(t, svc.deps.Cipher, "acc-v2")
	identity.onVerify = func() { repo.cred.Version++ }

	// ownsAlert=true：若实现仍无条件补发通知，这里会留下事件。
	svc.runAutoApprove(context.Background(), "acc-v2", "school-1", true)

	audits, ok := svc.deps.Audit.(*reviewFakeAudit)
	if !ok {
		t.Fatalf("audit fake = %T, want *reviewFakeAudit", svc.deps.Audit)
	}
	if repo.getCalls < 3 {
		t.Fatalf("Get calls = %d, want >= 3（冲突判定 + 复核各读一次）", repo.getCalls)
	}
	if count := auditActionCount(audits, "account.auto_approve"); count != 0 {
		t.Fatalf("auto approve audit rows = %d, want 0", count)
	}
	if count := auditActionCount(audits, "account.auto_check_failed"); count != 0 {
		t.Fatalf("auto check failed rows = %d, want 0（人工已通过，不写失败词）", count)
	}
	if len(alerts.events) != 0 {
		t.Fatalf("通知 = %+v, want 0（审计未落地不得发通知）", alerts.events)
	}
}

// TestRunAutoApproveVersionConflictAuditFailureStillAlerts 钉住 N-a：
// 审计写失败只 Warn，不改「行仍 pending、应补发通知」这一结论。runAutoApprove
// 无返回值，等价观测是：账号留 pending、恰好一条待审通知、审计只尝试一次。
func TestRunAutoApproveVersionConflictAuditFailureStillAlerts(t *testing.T) {
	repo := &autoRepo{record: Record{ID: "acc-v3", SchoolID: "school-1", Status: StatusPending}}
	identity := &fakeIdentity{profile: contract.Profile{UniversityID: "U1"}}
	alerts := &recordingReviewAlerts{}
	svc := newAutoServiceAlerts(t, repo,
		autoSchools{record: coreschool.Record{ID: "school-1", Status: coreschool.StatusActive, PlatformUniversityID: "U1"}},
		&autoSettings{enabled: true}, identity, alerts)
	repo.cred = sealedCredential(t, svc.deps.Cipher, "acc-v3")
	identity.onVerify = func() { repo.cred.Version++ }
	audits, ok := svc.deps.Audit.(*reviewFakeAudit)
	if !ok {
		t.Fatalf("audit fake = %T, want *reviewFakeAudit", svc.deps.Audit)
	}
	audits.appendErr = errors.New("audit down")

	svc.runAutoApprove(context.Background(), "acc-v3", "school-1", true)

	if repo.record.Status != StatusPending {
		t.Fatalf("status = %q, want pending", repo.record.Status)
	}
	if audits.attempts != 1 {
		t.Fatalf("audit attempts = %d, want 1", audits.attempts)
	}
	if count := auditActionCount(audits, "account.auto_check_failed"); count != 0 {
		t.Fatalf("auto check failed rows = %d, want 0（Append 失败不得记成已写）", count)
	}
	if len(alerts.events) != 1 {
		t.Fatalf("通知 = %+v, want 1（审计失败不得吞掉待审通知）", alerts.events)
	}
	if got := alerts.events[0].Category; got != corenotify.CategoryReviewPending {
		t.Fatalf("通知类别 = %q, want review_pending", got)
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

// newAutoServiceAlerts 与 newAutoService 同形，另注入审核通知端口。
// ReviewAlerts 是 *recordingReviewAlerts：断言自动审批链的通知次数与形状。
func newAutoServiceAlerts(t *testing.T, repo Repo, schools SchoolReader,
	settings AutoApproveSettings, identity *fakeIdentity,
	alerts ReviewAlerts) *Service {
	t.Helper()
	cipher, err := NewCipher(bytes.Repeat([]byte{9}, CredentialKeyBytes))
	if err != nil {
		t.Fatalf("NewCipher: %v", err)
	}
	svc, err := NewService(Deps{
		Accounts:     repo,
		Schools:      schools,
		Audit:        &reviewFakeAudit{},
		Tx:           reviewFakeTx{},
		Clock:        clock.Fixed{T: time.Date(2026, 11, 8, 6, 0, 0, 0, time.UTC)},
		Cipher:       cipher,
		Owner:        fakeOwnerReader{},
		Device:       &fakeDeviceReader{},
		Settings:     settings,
		Identity:     identity,
		Quota:        fixedQuota(10),
		ReviewAlerts: alerts,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

// P3-14：confirm_defaults+开关开、核验通过——只发一条 auto_approved 最终态，
// 不再 pending 与 auto_approved 各发一份。
func TestAutoApproveSuccessSendsOnlyFinalAlert(t *testing.T) {
	repo := &autoRepo{record: Record{ID: "acc-d1", SchoolID: "school-1", Status: StatusPending}}
	identity := &fakeIdentity{profile: contract.Profile{UniversityID: "U1"}}
	alerts := &recordingReviewAlerts{}
	svc := newAutoServiceAlerts(t, repo,
		autoSchools{record: coreschool.Record{ID: "school-1", Status: coreschool.StatusActive, PlatformUniversityID: "U1"}},
		&autoSettings{enabled: true}, identity, alerts)
	repo.cred = sealedCredential(t, svc.deps.Cipher, "acc-d1")

	if _, err := svc.Create(context.Background(), corescope.Master(), CreateInput{
		SchoolID: "school-1", Phone: "13800138061", Password: "secret", ConfirmDefaults: true,
	}, fakeActor()); err != nil {
		t.Fatalf("Create: %v", err)
	}
	svc.waitBackground()

	if repo.record.Status != StatusActive {
		t.Fatalf("status = %q, want active", repo.record.Status)
	}
	if len(alerts.events) != 1 {
		t.Fatalf("通知 = %d, want 1（只发最终态）：%+v", len(alerts.events), alerts.events)
	}
	event := alerts.events[0]
	if !event.AutoApproved || event.Category != corenotify.CategoryReviewPending ||
		event.DedupeKey != "account_auto_approve/acc-d1/2026-11-08" {
		t.Fatalf("事件 = %+v, want auto_approved 最终态、键含账号+日", event)
	}
}

// P3-14：核验失败留 pending——同一条通知是 pending 文案（最终态），仍只一条。
func TestAutoApproveFailureSendsOnlyPendingAlert(t *testing.T) {
	repo := &autoRepo{record: Record{ID: "acc-d2", SchoolID: "school-1", Status: StatusPending}}
	identity := &fakeIdentity{profile: contract.Profile{UniversityID: "U2"}}
	alerts := &recordingReviewAlerts{}
	svc := newAutoServiceAlerts(t, repo,
		autoSchools{record: coreschool.Record{ID: "school-1", Status: coreschool.StatusActive, PlatformUniversityID: "U1"}},
		&autoSettings{enabled: true}, identity, alerts)
	repo.cred = sealedCredential(t, svc.deps.Cipher, "acc-d2")

	if _, err := svc.Create(context.Background(), corescope.Master(), CreateInput{
		SchoolID: "school-1", Phone: "13800138062", Password: "secret", ConfirmDefaults: true,
	}, fakeActor()); err != nil {
		t.Fatalf("Create: %v", err)
	}
	svc.waitBackground()

	if repo.record.Status != StatusPending {
		t.Fatalf("status = %q, want pending", repo.record.Status)
	}
	if len(alerts.events) != 1 {
		t.Fatalf("通知 = %d, want 1（失败后 pending 一条）：%+v", len(alerts.events), alerts.events)
	}
	event := alerts.events[0]
	if event.AutoApproved || event.DedupeKey != "review_pending/acc-d2/2026-11-08" {
		t.Fatalf("事件 = %+v, want pending 键 review_pending/acc-d2/2026-11-08", event)
	}
}

// P3-14 负向：开关在 Create 与核验之间保持关闭——pending 通知仍恰一条
// （Create 发，核验早退不补发）。
func TestAutoApproveSwitchOffSendsSinglePendingAlert(t *testing.T) {
	repo := &autoRepo{record: Record{ID: "acc-d3", SchoolID: "school-1", Status: StatusPending}}
	identity := &fakeIdentity{}
	alerts := &recordingReviewAlerts{}
	svc := newAutoServiceAlerts(t, repo, autoSchools{}, &autoSettings{enabled: false}, identity, alerts)
	repo.cred = sealedCredential(t, svc.deps.Cipher, "acc-d3")

	if _, err := svc.Create(context.Background(), corescope.Master(), CreateInput{
		SchoolID: "school-1", Phone: "13800138063", Password: "secret", ConfirmDefaults: true,
	}, fakeActor()); err != nil {
		t.Fatalf("Create: %v", err)
	}
	svc.waitBackground()

	if identity.calls != 0 {
		t.Fatalf("platform calls = %d, want 0", identity.calls)
	}
	if len(alerts.events) != 1 || alerts.events[0].AutoApproved {
		t.Fatalf("通知 = %+v, want 恰一条 pending", alerts.events)
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
