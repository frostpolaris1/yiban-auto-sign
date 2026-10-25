// 忘记口令用例。

package auth

import (
	"context"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// ForgotPassword 为已知账号创建一次性重置令牌。
//
// **始终返回成功**：邮箱不存在时不写行、不回错误。
// 两条路径都付一次等价 KDF：否则响应时延暴露账号是否存在。
// 令牌只存 SHA-256，原始值不返回、不入日志。
// SMTP 未接线，令牌投递属后续通知批；故本动作**不写审计**：
// 没有投递就没有可审计事件，且 audit 词表不含该动作。
func (s *Service) ForgotPassword(ctx context.Context, email string) error {
	email = coreuser.NormalizeEmail(email)
	if !coreuser.ValidEmail(email) {
		return errs.New(errs.CodeValidation, "invalid email")
	}
	rec, err := s.users.GetByEmail(ctx, email)
	if err != nil {
		if isNotFound(err) {
			// 未知邮箱走与已知邮箱同量的垫时。
			if padErr := verifyDummyPassword("yiban-forgot-timing"); padErr != nil {
				return padErr
			}
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
	// 已知邮箱在令牌写入后付同一次垫时。
	// 垫时**不新增数据库写**：未知路径不得因为「等量工作」变成写路径。
	return verifyDummyPassword("yiban-forgot-timing")
}
