// 登出用例。

package auth

import (
	"context"

	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
)

// Logout 撤销当前会话并写 auth.logout。
//
// 撤销与会话审计同事务：**不留「已登出但无审计」**。
// 无令牌时是空操作；契约把无会话登出定义为 204。
func (s *Service) Logout(ctx context.Context, sess coreauth.Session, token string) error {
	if token == "" {
		return nil
	}
	now := s.clock.Now().UTC()
	return s.tx.InTx(ctx, func(ctx context.Context) error {
		if err := s.sessions.Revoke(ctx, token); err != nil {
			return err
		}
		_, err := s.audits.Append(ctx, coreaudit.Entry{
			TS:          now,
			ActorUserID: sess.User.ID,
			ActorRole:   sess.User.Role,
			ActorLabel:  mask.EmailLabel(sess.User.Email),
			Action:      coreaudit.ActionAuthLogout,
			TargetType:  "user",
			TargetID:    sess.User.ID,
		})
		return err
	})
}
