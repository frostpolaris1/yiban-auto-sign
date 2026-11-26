package account

// 平台探测的守卫单测（ykxf）。
//
// 判据：平台认证与设备读回都走注入端口；凭据不落账号仓储；
// 身份缺字段与平台失败统一 upstream；活跃校回填、disabled 校为 null，
// 普通用户探测未登记校按 not_found 原样上抛、不代建。

import (
	"context"
	"errors"
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
	record          coreschool.Record
	err             error
	calls           int
	lastID          string
	last            string
	lastAllowCreate bool
}

func (f *fakeProjector) EnsurePlatformSchool(_ context.Context, universityID, name string, allowCreate bool) (coreschool.Record, error) {
	f.calls++
	f.lastID, f.last, f.lastAllowCreate = universityID, name, allowCreate
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
	if projector.lastAllowCreate {
		t.Fatalf("ordinary user detection asked to create a school; it may only reuse a registered one")
	}
	if identity.lastReq.Credential.Phone != "13800138031" ||
		identity.lastReq.Credential.Password != "platform-secret" {
		t.Fatalf("platform request = %+v", identity.lastReq.Credential)
	}
	if repo.credCalls != 0 {
		t.Fatalf("detect read %d credentials; it must not persist or read account rows", repo.credCalls)
	}
}

// 建校权限按角色分流：普通用户只复用已登记学校，未登记按 not_found
// 原样上抛（由 school 服务拒绝代建、不写审计）；master/admin 保留自动建校。
func TestDetectSchoolCreationFollowsRole(t *testing.T) {
	active := coreschool.Record{
		ID: "school-33", Name: "示例大学", Status: coreschool.StatusActive,
		PlatformUniversityID: "0000000033",
	}
	cases := []struct {
		name            string
		scope           corescope.Scope
		projector       *fakeProjector
		wantErr         errs.Code
		wantSchool      bool
		wantAllowCreate bool
	}{
		{
			name:            "user reuses a registered school",
			scope:           corescope.User("user-1"),
			projector:       &fakeProjector{record: active},
			wantSchool:      true,
			wantAllowCreate: false,
		},
		{
			name:            "user cannot create an unregistered school",
			scope:           corescope.User("user-1"),
			projector:       &fakeProjector{err: errs.New(errs.CodeNotFound, "school is not registered; ask an administrator to create it")},
			wantErr:         errs.CodeNotFound,
			wantAllowCreate: false,
		},
		{
			name:            "admin still creates",
			scope:           corescope.Admin([]string{"school-33"}),
			projector:       &fakeProjector{record: active},
			wantSchool:      true,
			wantAllowCreate: true,
		},
		{
			name:            "master still creates",
			scope:           corescope.Master(),
			projector:       &fakeProjector{record: active},
			wantSchool:      true,
			wantAllowCreate: true,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			identity := &fakeIdentity{profile: contract.Profile{
				DisplayName: "测*生", SchoolName: "示例大学", UniversityID: "0000000033",
			}}
			svc, _ := newDetectService(t, identity, tc.projector, &fakeDeviceReader{})

			result, err := svc.Detect(context.Background(), tc.scope, DetectInput{
				Phone: "13800138033", Password: "platform-secret",
			})
			if tc.wantErr != "" {
				if err == nil || errs.From(err).Code != tc.wantErr {
					t.Fatalf("error = %v, want %s", err, tc.wantErr)
				}
				if result.School != nil {
					t.Fatalf("school = %+v, want null when lookups fail", result.School)
				}
			} else if err != nil {
				t.Fatalf("Detect: %v", err)
			}
			if tc.projector.calls != 1 || tc.projector.lastAllowCreate != tc.wantAllowCreate {
				t.Fatalf("projection calls = %d allowCreate = %t, want 1/%t",
					tc.projector.calls, tc.projector.lastAllowCreate, tc.wantAllowCreate)
			}
			if tc.wantSchool && (result.School == nil || result.School.ID != "school-33") {
				t.Fatalf("school = %+v, want the registered school", result.School)
			}
		})
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

// 急停门错误必须原样上抛：detect 入口据此回 503，不得伪装成平台故障。
func TestDetectPassesThroughPausedGate(t *testing.T) {
	identity := &fakeIdentity{err: errs.NewPaused()}
	svc, _ := newDetectService(t, identity, &fakeProjector{}, &fakeDeviceReader{})

	_, err := svc.Detect(context.Background(), corescope.Master(), DetectInput{
		Phone: "13800138036", Password: "platform-secret",
	})
	var paused *errs.PausedError
	if err == nil || !errors.As(err, &paused) {
		t.Fatalf("error = %v, want paused", err)
	}

	// 设备读回同样透传：认证成功但设备阶段被急停拒绝。
	identity = &fakeIdentity{profile: contract.Profile{
		DisplayName: "测*生", SchoolName: "示例大学", UniversityID: "0000000031",
	}}
	device := &fakeDeviceReader{err: errs.NewPaused()}
	svc, _ = newDetectService(t, identity, &fakeProjector{}, device)
	_, err = svc.Detect(context.Background(), corescope.Master(), DetectInput{
		Phone: "13800138037", Password: "platform-secret",
	})
	if err == nil || !errors.As(err, &paused) {
		t.Fatalf("device error = %v, want paused", err)
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
