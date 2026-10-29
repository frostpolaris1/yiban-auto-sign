// 忘记口令用例。

package auth

import (
	"context"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// ForgotPassword 为已知账号创建一次性重置令牌，并投递重置链接。
//
// **始终返回成功**：邮箱不存在时不写行、不回错误。
// 三个成功分支都付一次等价 KDF：未知邮箱、被限速、正常投递。
// 否则响应时延暴露账号是否存在，或暴露"这个邮箱刚被申请过"。
//
// 同一归一邮箱在 ForgotCooldown 内只产生一个令牌与一封邮件；
// 被限速的请求仍返回成功、仍付垫时。
//
// 令牌只存 SHA-256；原始值只出现在邮件正文，不返回调用方、不入日志。
// 投递在后台进行，未配置 SMTP 时记一条显性「未投递」警告。
// 本动作仍**不写审计**：audit 词表是安全动作的闭集，
// 投递结果属运维事件，走结构化日志（event=password_reset.*）。
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
	now := s.clock.Now().UTC()
	if !s.throttle.allow(email, now) {
		s.log.Info("password reset request throttled",
			"event", "password_reset.throttled",
			"email", mask.EmailLabel(email))
		// 被限速的请求不得比正常请求便宜：同样付一次 KDF。
		return verifyDummyPassword("yiban-forgot-timing")
	}
	token, err := coreauth.NewToken()
	if err != nil {
		return err
	}
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
	if padErr := verifyDummyPassword("yiban-forgot-timing"); padErr != nil {
		return padErr
	}
	// 投递在后台进行；失败不改变响应（见 deliverReset）。
	s.deliverReset(rec.Email, token)
	return nil
}
