// 忘记口令用例。

package auth

import (
	"context"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

// ForgotPassword 为已知账号创建一次性重置令牌。
//
// **始终返回成功**：邮箱不存在时不写行、不回错误。
// 令牌只存 SHA-256，原始值不返回、不入日志。
// SMTP 未接线，令牌投递属后续通知批；故本动作**不写审计**：
// 没有投递就没有可审计事件，且 audit 词表不含该动作。
func (s *Service) ForgotPassword(ctx context.Context, email string) error {
	email = normalizeEmail(email)
	if !validEmail(email) {
		return errs.New(errs.CodeValidation, "invalid email")
	}
	rec, err := s.users.GetByEmail(ctx, email)
	if err != nil {
		if isNotFound(err) {
			return nil
		}
		return err
	}
	token, err := coreauth.NewToken()
	if err != nil {
		return err
	}
	now := s.clock.Now().UTC()
	if _, err := s.resets.Create(ctx, coreauth.NewPasswordReset{
		UserID:    rec.ID,
		TokenHash: coreauth.HashToken(token),
		Now:       now,
		ExpiresAt: now.Add(ResetTokenTTL),
	}); err != nil {
		return err
	}
	return nil
}
