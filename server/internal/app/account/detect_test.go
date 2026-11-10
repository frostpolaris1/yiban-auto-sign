package account

// 平台探测的守卫单测（ykxf）。
//
// 判据：平台认证与设备读回都走注入端口；凭据不落账号仓储；
// 身份缺字段与平台失败统一 upstream；活跃校回填、disabled 校为 null。

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/school"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

type fakeIdentity struct {
	profile contract.Profile
	err     error
	calls   int
	lastReq contract.VerifyRequest
}

func (f *fakeIdentity) Verify(_ context.Context, req contract.VerifyRequest) (contract.Profile, error) {
	f.calls++
	f.lastReq = req
	return f.profile, f.err
}

type fakeProjector struct {
	record coreschool.Record
	err    error
	calls  int
	lastID string
	last   string
}

func (f *fakeProjector) EnsurePlatformSchool(_ context.Context, universityID, name string) (coreschool.Record, error) {
	f.calls++
	f.lastID, f.last = universityID, name
	return f.record, f.err
}

func newDetectService(t *testing.T, identity *fakeIdentity, projector *fakeProjector, device *fakeDeviceReader) (*Service, *fakeAccountRepo) {
	t.Helper()
	repo := &fakeAccountRepo{}
	svc, err := NewService(Deps{
		Accounts:         repo,
		Schools:          fakeSchoolReader{},
		Audit:            fakeAuditRepo{},
		Tx:               fakeTxManager{},
		Clock:            clock.Fixed{T: time.Date(2026, 11, 8, 6, 0, 0, 0, time.UTC)},
		Cipher:           nil,
		Owner:            fakeOwnerReader{},
		Device:           device,
		Identity:         identity,
		SchoolProjection: projector,
		Quota:            fixedQuota(10),
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc, repo
}

func TestDetectReturnsIdentityAndSchoolWithoutPersisting(t *testing.T) {
	identity := &fakeIdentity{profile: contract.Profile{
		DisplayName: "测*生", SchoolName: "示例大学", UniversityID: "0000000031",
	}}
	projector := &fakeProjector{record: coreschool.Record{
		ID: "school-31", Name: "示例大学", Status: coreschool.StatusActive,
		PlatformUniversityID: "0000000031",
	}}
	device := &fakeDeviceReader{state: DeviceState{
		Authorized: true, Code: "DEVICE-SECRET-31", PhoneModel: "Mi 9",
	}}
	svc, repo := newDetectService(t, identity, projector, device)

	result, err := svc.Detect(context.Background(), corescope.User("user-1"), DetectInput{
		Phone: "13800138031", Password: "platform-secret",
	})
	if err != nil {
		t.Fatalf("Detect: %v", err)
	}
	if result.UniversityName != "示例大学" || result.UniversityID != "0000000031" {
		t.Fatalf("identity = %+v", result)
	}
	if result.School == nil || result.School.ID != "school-31" || result.School.Name != "示例大学" {
		t.Fatalf("school = %+v", result.School)
	}
	if result.PersonName != "测*生" || result.DeviceModel != "Mi 9" || !result.DeviceCodePresent {
		t.Fatalf("result = %+v", result)
	}
	if projector.lastID != "0000000031" || projector.last != "示例大学" {
		t.Fatalf("projection input = %q %q", projector.lastID, projector.last)
	}
	if identity.lastReq.Credential.Phone != "13800138031" ||
		identity.lastReq.Credential.Password != "platform-secret" {
		t.Fatalf("platform request = %+v", identity.lastReq.Credential)
	}
	if repo.credCalls != 0 {
		t.Fatalf("detect read %d credentials; it must not persist or read account rows", repo.credCalls)
	}
}

func TestDetectDisabledSchoolReturnsNull(t *testing.T) {
	identity := &fakeIdentity{profile: contract.Profile{
		DisplayName: "测*生", SchoolName: "停用大学", UniversityID: "0000000032",
	}}
	projector := &fakeProjector{record: coreschool.Record{
		ID: "school-32", Name: "停用大学", Status: coreschool.StatusDisabled,
		PlatformUniversityID: "0000000032",
	}}
	svc, _ := newDetectService(t, identity, projector, &fakeDeviceReader{})

	result, err := svc.Detect(context.Background(), corescope.Master(), DetectInput{
		Phone: "13800138032", Password: "platform-secret",
	})
	if err != nil {
		t.Fatalf("Detect: %v", err)
	}
	if result.School != nil {
		t.Fatalf("disabled school = %+v, want null", result.School)
	}
}

func TestDetectPlatformFailureIsNeutralUpstream(t *testing.T) {
	identity := &fakeIdentity{err: errs.New(errs.CodeUpstream, "upstream exploded with 13900139000")}
	svc, _ := newDetectService(t, identity, &fakeProjector{}, &fakeDeviceReader{})

	_, err := svc.Detect(context.Background(), corescope.Master(), DetectInput{
		Phone: "13800138033", Password: "platform-secret",
	})
	if err == nil || errs.From(err).Code != errs.CodeUpstream {
		t.Fatalf("error = %v, want upstream", err)
	}
	if !strings.Contains(err.Error(), "platform detection failed") {
		t.Fatalf("error text = %q, want the neutral message", err.Error())
	}
	if strings.Contains(err.Error(), "13900139000") {
		t.Fatalf("error leaked platform text: %v", err)
	}
}

func TestDetectIncompleteIdentityIsUpstream(t *testing.T) {
	identity := &fakeIdentity{profile: contract.Profile{DisplayName: "测*生"}}
	projector := &fakeProjector{}
	svc, _ := newDetectService(t, identity, projector, &fakeDeviceReader{})

	_, err := svc.Detect(context.Background(), corescope.Master(), DetectInput{
		Phone: "13800138034", Password: "platform-secret",
	})
	if err == nil || errs.From(err).Code != errs.CodeUpstream {
		t.Fatalf("error = %v, want upstream", err)
	}
	if projector.calls != 0 {
		t.Fatalf("incomplete identity created %d schools, want 0", projector.calls)
	}
}

func TestDetectAbnormalPlatformIdentityIsUpstream(t *testing.T) {
	cases := []contract.Profile{
		{UniversityID: "   ", SchoolName: "示例大学"},
		{UniversityID: "大学-01", SchoolName: "示例大学"},
		{UniversityID: "0000 01", SchoolName: "示例大学"},
		{UniversityID: "-0001", SchoolName: "示例大学"},
		{UniversityID: strings.Repeat("a", 129), SchoolName: "示例大学"},
		{UniversityID: "0000000034", SchoolName: "   "},
	}
	for _, profile := range cases {
		identity := &fakeIdentity{profile: profile}
		projector := &fakeProjector{}
		svc, _ := newDetectService(t, identity, projector, &fakeDeviceReader{})

		_, err := svc.Detect(context.Background(), corescope.Master(), DetectInput{
			Phone: "13800138034", Password: "platform-secret",
		})
		if err == nil || errs.From(err).Code != errs.CodeUpstream {
			t.Fatalf("profile %+v error = %v, want upstream", profile, err)
		}
		if projector.calls != 0 {
			t.Fatalf("profile %+v created %d schools, want 0", profile, projector.calls)
		}
	}
}

func TestDetectTrimsPlatformIdentity(t *testing.T) {
	identity := &fakeIdentity{profile: contract.Profile{
		DisplayName: "测*生", SchoolName: "  示例大学  ", UniversityID: " 0000000035 ",
	}}
	projector := &fakeProjector{record: coreschool.Record{
		ID: "school-35", Name: "示例大学", Status: coreschool.StatusActive,
	}}
	svc, _ := newDetectService(t, identity, projector, &fakeDeviceReader{})

	result, err := svc.Detect(context.Background(), corescope.Master(), DetectInput{
		Phone: "13800138035", Password: "platform-secret",
	})
	if err != nil {
		t.Fatalf("Detect: %v", err)
	}
	if result.UniversityID != "0000000035" || result.UniversityName != "示例大学" {
		t.Fatalf("result = %+v", result)
	}
	if projector.lastID != "0000000035" || projector.last != "示例大学" {
		t.Fatalf("projection input = %q %q", projector.lastID, projector.last)
	}
}

func TestDetectValidatesPhoneAndPassword(t *testing.T) {
	svc, _ := newDetectService(t, &fakeIdentity{}, &fakeProjector{}, &fakeDeviceReader{})

	cases := []DetectInput{
		{Phone: "123", Password: "x"},
		{Phone: "13800138035", Password: ""},
	}
	for _, in := range cases {
		_, err := svc.Detect(context.Background(), corescope.Master(), in)
		if err == nil || errs.From(err).Code != errs.CodeValidation {
			t.Fatalf("input %+v error = %v, want validation", in, err)
		}
	}
}
