package auth

// 公开注册与注册开关。
//
// # 功能
// 读注册状态、开关注册、公开注册建号并签发会话。
// 本文件**不**写 SQL，**不**做 HTTP 编码。
//
// # 归属
// app 层用例。开关读写经 RegistrationStore；建号经 app/user 端口；
// 会话经 core/auth.SessionIssuer。
//
// # 口径
// 注册默认关闭；开关值只有 true 视为开放。
// 关闭时在付出口令哈希之前返回 403，且不写审计。
// 注册成功建 role=user、status=active、must_change_password=false 的账号，
// 并当场签发会话：建号、会话与两条审计同一事务提交。
// 协议勾选两个布尔都必须为 true，且版本必须等于当前常量；
// 陈旧版本回 409，提示调用方重新读取状态后重试。
// 邮箱归一与形状校验取自 core/user；重复邮箱回 409，文案不区分大小写变体。
// 口令强度取自 core/auth；与旧线的 10 位、两类一致。

import (
	"context"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// 协议版本。文案改动即加一；注册请求必须带上当前值。
const (
	TermsVersion   = 1
	PrivacyVersion = 1
)

// RegistrationStatus 是公开的注册状态。
// 版本号始终返回，关闭时也一样：界面可先展示协议，等开放后直接提交。
type RegistrationStatus struct {
	Enabled        bool
	TermsVersion   int
	PrivacyVersion int
}

// RegisterInput 是公开注册的输入。
type RegisterInput struct {
	Email          string
	Password       string
	AgreeTerms     bool
	AgreePrivacy   bool
	TermsVersion   int
	PrivacyVersion int
}

// Registration 读注册状态。
// 存储故障原样上抛，**不**当作关闭：关闭是业务状态，故障是故障。
func (s *Service) Registration(ctx context.Context) (RegistrationStatus, error) {
	enabled, err := s.registrations.RegistrationEnabled(ctx)
	if err != nil {
		return RegistrationStatus{}, err
	}
	return RegistrationStatus{
		Enabled:        enabled,
		TermsVersion:   TermsVersion,
		PrivacyVersion: PrivacyVersion,
	}, nil
}

// SetRegistration 打开或关闭公开注册。**只有 master 可以调用**。
// 开关写入与 settings.registration 审计同事务。
func (s *Service) SetRegistration(ctx context.Context, scope corescope.Scope, actor appaudit.Actor, enabled bool) (RegistrationStatus, error) {
	if err := app.RequireScope(scope); err != nil {
		return RegistrationStatus{}, err
	}
	if scope.Kind() != corescope.KindMaster {
		return RegistrationStatus{}, errs.New(errs.CodeForbidden, "master role is required")
	}
	target := "disabled"
	if enabled {
		target = "enabled"
	}
	err := s.tx.InTx(ctx, func(ctx context.Context) error {
		if err := s.registrations.SetRegistrationEnabled(ctx, enabled, actor.UserID); err != nil {
			return err
		}
		_, err := s.audits.Append(ctx, coreaudit.Entry{
			TS:          s.clock.Now().UTC(),
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionSettingsRegistration,
			TargetType:  "settings",
			TargetID:    "registration",
			Detail:      map[string]any{"field": "registration", "to": target},
		})
		return err
	})
	if err != nil {
		return RegistrationStatus{}, err
	}
	return RegistrationStatus{
		Enabled:        enabled,
		TermsVersion:   TermsVersion,
		PrivacyVersion: PrivacyVersion,
	}, nil
}

// Register 创建普通用户并签发会话。
//
// 判定顺序：开关 → 邮箱 → 协议 → 口令；开关关闭时不付 KDF。
// 建号、会话（含 auth.login）与 auth.register 同事务：
// 任一步失败都不留下半套账号或会话。
func (s *Service) Register(ctx context.Context, in RegisterInput) (coreauth.Issued, error) {
	enabled, err := s.registrations.RegistrationEnabled(ctx)
	if err != nil {
		return coreauth.Issued{}, err
	}
	if !enabled {
		return coreauth.Issued{}, errs.New(errs.CodeForbidden, "registration is disabled")
	}
	email := coreuser.NormalizeEmail(in.Email)
	if !coreuser.ValidEmail(email) {
		return coreauth.Issued{}, errs.New(errs.CodeValidation, "invalid email")
	}
	if !in.AgreeTerms || !in.AgreePrivacy {
		return coreauth.Issued{}, errs.New(errs.CodeValidation, "agreement to the terms and privacy policy is required")
	}
	if in.TermsVersion <= 0 || in.PrivacyVersion <= 0 {
		return coreauth.Issued{}, errs.New(errs.CodeValidation, "terms_version and privacy_version are required")
	}
	if in.TermsVersion != TermsVersion || in.PrivacyVersion != PrivacyVersion {
		return coreauth.Issued{}, errs.New(errs.CodeConflict, "agreement version changed; reload and try again")
	}
	if reason := coreauth.PasswordPolicyError(in.Password); reason != "" {
		return coreauth.Issued{}, errs.New(errs.CodeValidation, reason)
	}

	hash, err := s.kdf.Hash(ctx, in.Password)
	if err != nil {
		return coreauth.Issued{}, err
	}

	now := s.clock.Now().UTC()
	var issued coreauth.Issued
	err = s.tx.InTx(ctx, func(ctx context.Context) error {
		record, err := s.users.Create(ctx, coreuser.New{
			Email:        email,
			PasswordHash: hash,
			Role:         coreauth.RoleUser,
		}, s.userLimit)
		if err != nil {
			if errs.From(err).Code == errs.CodeConflict {
				return errs.New(errs.CodeConflict, "email is already registered")
			}
			return err
		}
		issued, err = s.sessions.Issue(ctx, coreauth.User{
			ID:                 record.ID,
			Email:              record.Email,
			Role:               record.Role,
			Status:             record.Status,
			MustChangePassword: record.MustChangePassword,
		})
		if err != nil {
			return err
		}
		// detail 只记协议版本：邮箱是 PII，不得进审计。
		_, err = s.audits.Append(ctx, coreaudit.Entry{
			TS:          now,
			ActorUserID: record.ID,
			ActorRole:   record.Role,
			ActorLabel:  mask.EmailLabel(record.Email),
			Action:      coreaudit.ActionAuthRegister,
			TargetType:  "user",
			TargetID:    record.ID,
			Detail: map[string]any{
				"terms_version":   TermsVersion,
				"privacy_version": PrivacyVersion,
			},
		})
		return err
	})
	if err != nil {
		return coreauth.Issued{}, err
	}
	return issued, nil
}
