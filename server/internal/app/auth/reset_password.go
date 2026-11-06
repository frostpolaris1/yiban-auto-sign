// 重置口令用例。

package auth

import (
	"context"

	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
)

// ResetPassword 用一次性令牌替换口令。
//
// 以下六步同属一个事务：消费令牌、读用户、判口令策略、写新哈希、
// 撤销全部会话、写审计。任一步失败即整体回滚，
// 故令牌不会「消费了但口令没改」。
// 令牌未知、已用或过期统一返回 ErrInvalidResetToken。
// 新口令按令牌对应用户的角色档判强度策略，一律 argon2id，
// 且 must_change_password 置 false。
func (s *Service) ResetPassword(ctx context.Context, token, newPassword string) error {
	if token == "" {
		return errs.New(errs.CodeValidation, "token is required")
	}
	if newPassword == "" {
		return errs.New(errs.CodeValidation, "new_password is required")
	}
	// 先验令牌：未知、已用或过期的令牌不付 Argon2 成本。
	now := s.clock.Now().UTC()
	if _, err := s.resets.Peek(ctx, coreauth.HashToken(token), now); err != nil {
		if isNotFound(err) {
			return ErrInvalidResetToken
		}
		return err
	}
	// 哈希在事务外算：argon2id 约几十毫秒，不该占住行锁与链锁。
	newHash, err := s.kdf.Hash(ctx, newPassword)
	if err != nil {
		return err
	}
	return s.tx.InTx(ctx, func(ctx context.Context) error {
		reset, err := s.resets.Consume(ctx, coreauth.HashToken(token), now)
		if err != nil {
			if isNotFound(err) {
				return ErrInvalidResetToken
			}
			return err
		}
		rec, err := s.users.GetByID(ctx, reset.UserID)
		if err != nil {
			if isNotFound(err) {
				return ErrInvalidResetToken
			}
			return err
		}
		// 新口令按令牌对应用户的角色档判强度策略。事务内判：
		// 回滚即零写入，弱口令不会落库。
		if reason := coreauth.PasswordPolicyErrorForRole(newPassword, rec.Role); reason != "" {
			return errs.New(errs.CodeValidation, reason)
		}
		if err := s.users.UpdatePassword(ctx, rec.ID, newHash, false, now); err != nil {
			return err
		}
		if err := s.sessions.RevokeAllForUser(ctx, rec.ID); err != nil {
			return err
		}
		_, err = s.audits.Append(ctx, coreaudit.Entry{
			TS:          now,
			ActorUserID: rec.ID,
			ActorRole:   rec.Role,
			ActorLabel:  mask.EmailLabel(rec.Email),
			Action:      coreaudit.ActionAuthPasswordReset,
			TargetType:  "user",
			TargetID:    rec.ID,
		})
		return err
	})
}
