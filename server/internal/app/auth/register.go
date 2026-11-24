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
	// TurnstileSiteKey 为空表示人机验证关闭；非空时前端用它渲染控件，
	// 并把得到的令牌作为 RegisterInput.TurnstileToken 提交。
	// 它是公开值，可以出现在响应与页面源码里。
	TurnstileSiteKey string
	// TurnstileEnforced 表示服务端会真的拦：true 即 on 档，缺令牌或令牌
	// 无效一律 400 human_verification_failed；false 即 log 档，只记日志放行。
	// 前端据此决定「没有令牌就不许提交」，不再拿站点密钥猜档位。
	TurnstileEnforced bool
}

// RegisterInput 是公开注册的输入。
type RegisterInput struct {
	Email          string
	Password       string
	AgreeTerms     bool
	AgreePrivacy   bool
	TermsVersion   int
	PrivacyVersion int
	// TurnstileToken 是人机验证令牌；只在档位非 off 时被读取。
	// 不是 PII，但等同一次性凭据：不落库、不进审计与日志。
	TurnstileToken string
	// RemoteIP 是注册请求的客户端地址，供 siteverify 归因；可为空。
	RemoteIP string
}

// Registration 读注册状态。
// 存储故障原样上抛，**不**当作关闭：关闭是业务状态，故障是故障。
func (s *Service) Registration(ctx context.Context) (RegistrationStatus, error) {
	enabled, err := s.registrations.RegistrationEnabled(ctx)
	if err != nil {
		return RegistrationStatus{}, err
	}
	return s.registrationStatus(enabled), nil
}

// registrationStatus 组装对外状态。开关读写两个入口共用，
// 站点密钥只在人机验证启用（Human 非 nil）时回给前端。
func (s *Service) registrationStatus(enabled bool) RegistrationStatus {
	return RegistrationStatus{
		Enabled:           enabled,
		TermsVersion:      TermsVersion,
		PrivacyVersion:    PrivacyVersion,
		TurnstileSiteKey:  s.humanSiteKey,
		TurnstileEnforced: s.humanEnforce,
	}
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
	return s.registrationStatus(enabled), nil
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
	// 人机验证排在 KDF 之前：拒绝的请求不付哈希成本。
	// 关闭档不调用端口；log 档只记日志，不改变注册结果。
	if err := s.verifyHuman(ctx, in.TurnstileToken, in.RemoteIP); err != nil {
		return coreauth.Issued{}, err
	}

	hash, err := s.kdf.Hash(ctx, in.Password)
	if err != nil {
		return coreauth.Issued{}, err
	}
	// 配额在开事务前解析：读故障直接上抛，不当作不限；
	// 设置页写回后下一次注册即用新值。
	userLimit, err := s.quota.UserLimit(ctx)
	if err != nil {
		return coreauth.Issued{}, err
	}

	var issued coreauth.Issued
	err = s.tx.InTx(ctx, func(ctx context.Context) error {
		// 恢复权保护：在**建号同一事务**里、按邮箱取 advisory lock 后
		// 复核宽限内本人软删行。锁与 SoftDeleteSelf 共用，故注销未提交时
		// 本事务等锁；提交后 EXISTS 能看到行并回 409。锁在事务结束释放，
		// 从复核到 INSERT 之间不存在注册抢跑窗口。
		// 与旧线同口径：只拦公开注册，超窗后邮箱释放；管理员删除的行
		// 不属于用户反悔范围，不占邮箱。
		if err := s.users.LockEmail(ctx, email); err != nil {
			return err
		}
		now := s.clock.Now().UTC()
		blocked, err := s.users.HasSelfDeletedEmailWithin(ctx, email, now.Add(-coreuser.SelfRestoreGrace))
		if err != nil {
			return err
		}
		if blocked {
			return errs.New(errs.CodeConflict, "email is already registered")
		}
		record, err := s.users.Create(ctx, coreuser.New{
			Email:        email,
			PasswordHash: hash,
			Role:         coreauth.RoleUser,
		}, userLimit)
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
