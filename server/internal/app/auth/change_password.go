// 登录态自助改密用例。

package auth

import (
	"context"
	"fmt"

	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
)

// ErrPasswordUnchanged 表示新口令与当前口令相同。
// 它**不是**口令错误：调用方已通过旧口令校验。
var ErrPasswordUnchanged = errs.New(errs.CodeConflict, "new password must differ from the current password")

// ChangePassword 校验旧口令并替换为新口令。
//
// 以下五步同属一个事务：读用户、校验旧口令、写新哈希、
// 撤销本人其余会话、写审计。任一步失败即整体回滚。
// 旧口令错误与用户消失都返回 ErrInvalidCredentials，**不区分**。
// 新口令等于旧口令返回 ErrPasswordUnchanged。
// keepToken 对应的当前会话**保留**，其余会话全部撤销。
// 成功后 must_change_password 置 false，password_changed_at 写本次时刻。
func (s *Service) ChangePassword(ctx context.Context, sess coreauth.Session, keepToken, currentPassword, newPassword string) error {
	if currentPassword == "" {
		return errs.New(errs.CodeValidation, "current_password is required")
	}
	if newPassword == "" {
		return errs.New(errs.CodeValidation, "new_password is required")
	}
	if keepToken == "" {
		// 入口层已从 cookie 取到令牌；空值即接线错误，不是用户输入问题。
		return errs.New(errs.CodeInternal, "session token is required")
	}
	// 哈希在事务外算：argon2id 约几十毫秒，不该占住行锁与链锁。
	newHash, err := s.kdf.Hash(ctx, newPassword)
	if err != nil {
		return err
	}
	now := s.clock.Now().UTC()
	return s.tx.InTx(ctx, func(ctx context.Context) error {
		rec, err := s.users.GetByID(ctx, sess.User.ID)
		if err != nil {
			if isNotFound(err) {
				// 用户消失走与坏口令同量的垫时，两条路径不可区分。
				if padErr := s.verifyDummyPassword(ctx, currentPassword); padErr != nil {
					return padErr
				}
				return ErrInvalidCredentials
			}
			return err
		}
		check, err := s.kdf.Verify(ctx, currentPassword, rec.PasswordHash)
		if err != nil {
			// 坏哈希是数据事故，不是口令错误。
			return fmt.Errorf("appauth: stored hash for %s is unreadable: %w", rec.ID, err)
		}
		if !check.Valid || rec.Status != coreauth.StatusActive {
			return ErrInvalidCredentials
		}
		// 比较在旧口令校验之后：口令错时**永不**回 409。
		if newPassword == currentPassword {
			return ErrPasswordUnchanged
		}
		if err := s.users.UpdatePassword(ctx, rec.ID, newHash, false, now); err != nil {
			return err
		}
		if err := s.others.RevokeOthers(ctx, rec.ID, coreauth.HashToken(keepToken), now); err != nil {
			return err
		}
		_, err = s.audits.Append(ctx, coreaudit.Entry{
			TS:          now,
			ActorUserID: rec.ID,
			ActorRole:   rec.Role,
			ActorLabel:  mask.EmailLabel(rec.Email),
			Action:      coreaudit.ActionAuthPasswordChange,
			TargetType:  "user",
			TargetID:    rec.ID,
		})
		return err
	})
}
