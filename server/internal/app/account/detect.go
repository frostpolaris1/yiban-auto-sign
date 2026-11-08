package account

// 平台探测用例（ykxf）。
//
// # 功能
// 用提交的手机号与口令做一次平台认证，读回设备登记，再把平台院校
// 投影到本地学校。凭据只在本进程内交给平台端口：不落库、不进日志、
// 不进审计、不回响应；设备码只以状态位出现。
//
// # 归属
// app 层用例。IdentityVerifier 与 DeviceReader 由组合根注入 provider；
// PlatformSchoolProjector 由 app/school 注入，避免账号用例直接建校。
//
// # 口径
// 平台失败（含身份字段不全）统一 upstream；入口层映射为中性 502。
// disabled 绑定校视同未命中：不建第二所校，响应 school 为 null。

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"strings"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/school"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// IdentityVerifier 执行一次平台认证并返回非秘密身份。
// 实现是 signing/providers/yiban.Provider；接口复用签到链的 contract 形状。
type IdentityVerifier interface {
	Verify(ctx context.Context, req contract.VerifyRequest) (contract.Profile, error)
}

// PlatformSchoolProjector 把平台院校标识投影到本地学校。
// 活跃命中或自动建校返回 active 行；disabled 命中原样返回 disabled 行。
type PlatformSchoolProjector interface {
	EnsurePlatformSchool(ctx context.Context, universityID, name string) (coreschool.Record, error)
}

// DetectInput 是平台探测的输入。凭据只在本次调用内使用。
type DetectInput struct {
	Phone    string
	Password string
}

// DetectSchool 是探测结果里的学校摘要。
type DetectSchool struct {
	ID   string
	Name string
}

// DetectResult 是探测的公开结果。
// DeviceCodePresent 只是状态位；设备码本体不出用例层。
type DetectResult struct {
	UniversityName    string
	UniversityID      string
	School            *DetectSchool
	PersonName        string
	DeviceModel       string
	DeviceCodePresent bool
}

// Detect 执行平台探测。
//
// 合成会话键用于复用同一 provider 会话：认证一次，随后设备读回
// 命中同一会话缓存。键由手机号摘要生成（不含明文），同一号码的
// 探测共用一个缓存槽，进程内槽位数量与平台账号数同阶。
// 平台错误不向上透传细节，只回中性 upstream。
func (s *Service) Detect(ctx context.Context, scope corescope.Scope, in DetectInput) (DetectResult, error) {
	if err := app.RequireScope(scope); err != nil {
		return DetectResult{}, err
	}
	switch scope.Kind() {
	case corescope.KindMaster, corescope.KindAdmin, corescope.KindUser:
	default:
		return DetectResult{}, errs.New(errs.CodeForbidden, "role cannot detect platform identity")
	}
	if !phonePattern.MatchString(in.Phone) {
		return DetectResult{}, errs.New(errs.CodeValidation, "phone must match ^1[0-9]{10}$")
	}
	if err := validatePassword(in.Password); err != nil {
		return DetectResult{}, err
	}
	if s.deps.Identity == nil || s.deps.Device == nil || s.deps.SchoolProjection == nil {
		return DetectResult{}, errs.New(errs.CodeInternal, "platform detection is not wired")
	}

	// 会话键不含手机号或口令；设备读回与认证共用它。
	ref := detectSessionRef(in.Phone)
	profile, err := s.deps.Identity.Verify(ctx, contract.VerifyRequest{
		Account: contract.AccountRef{ID: ref, Platform: "yiban"},
		Credential: contract.Credential{
			Phone:    in.Phone,
			Password: in.Password,
		},
	})
	if err != nil {
		return DetectResult{}, errs.New(errs.CodeUpstream, "platform detection failed")
	}
	// 平台身份是外部数据：空白、非 ASCII 标识与超长值都按平台异常处理，
	// 统一中性 502，不让它们落到本地 400 或撞审计形状规则。
	universityID := strings.TrimSpace(profile.UniversityID)
	universityName := strings.TrimSpace(profile.SchoolName)
	if universityName == "" || !validPlatformUniversityID(universityID) {
		return DetectResult{}, errs.New(errs.CodeUpstream, "platform detection failed")
	}

	device, err := s.deps.Device.DeviceState(ctx, ref, SigningCredential{
		Phone:    in.Phone,
		Password: in.Password,
	})
	if err != nil {
		return DetectResult{}, errs.New(errs.CodeUpstream, "platform detection failed")
	}

	school, err := s.deps.SchoolProjection.EnsurePlatformSchool(ctx, universityID, universityName)
	if err != nil {
		return DetectResult{}, err
	}
	result := DetectResult{
		UniversityName:    universityName,
		UniversityID:      universityID,
		PersonName:        profile.DisplayName,
		DeviceModel:       truncateCredentialText(device.PhoneModel),
		DeviceCodePresent: device.Code != "",
	}
	if school.Status == coreschool.StatusActive {
		result.School = &DetectSchool{ID: school.ID, Name: school.Name}
	}
	return result, nil
}

// detectSessionRef 返回平台会话缓存键：手机号摘要，无明文、无口令。
// provider 的会话表按账号 ID 增长；用稳定摘要避免每次探测新增槽位。
func detectSessionRef(phone string) string {
	sum := sha256.Sum256([]byte("detect:" + phone))
	return "detect:" + hex.EncodeToString(sum[:16])
}

// validPlatformUniversityID 与审计 detail 的 university_id 形状同口径：
// 1..128 位 ASCII 字母数字，首字符必须是字母或数字，其后可带 _ 与 -。
// 非 ASCII、空白或标点都视为平台异常，由调用方映射为中性 502。
func validPlatformUniversityID(id string) bool {
	if len(id) == 0 || len(id) > 128 {
		return false
	}
	for i := 0; i < len(id); i++ {
		c := id[i]
		alnum := c >= '0' && c <= '9' || c >= 'A' && c <= 'Z' || c >= 'a' && c <= 'z'
		if i == 0 {
			if !alnum {
				return false
			}
			continue
		}
		if !alnum && c != '_' && c != '-' {
			return false
		}
	}
	return true
}
