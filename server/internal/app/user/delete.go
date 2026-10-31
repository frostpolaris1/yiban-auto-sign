// 软删用户用例。
//
// # 功能
// 只实现 DELETE /api/users/{id} 对应的删除动作：
// 拦名下活跃账号、写软删标记、撤销全部会话、写审计。
// 本包**不**做 HTTP 编码、**不**删任何账号。
//
// # 归属
// app 层用例。端口见 repo.go 与 OwnerAccountCounter，实现见 adapters/pg。
//
// # 口径
// 只有 master 可以删用户。目标是自己 403：软删自己即全端失效，
// 操作者失去恢复手段。
// 名下还有活跃账号 409：先转移或删号。软删用户的账号不在可见面，
// 这条是唯一入口判据。
// 事务内＝GetByID + 计数 + SoftDelete + RevokeAllForUser + 审计。
// 解析层 JOIN 已判 deleted_at；显式撤销让 sessions.revoked_at 有据可查。

package user

import (
	"context"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// Delete 软删一个用户。**只有 master 可以调用**；目标是自己 403。
func (s *Service) Delete(ctx context.Context, scope corescope.Scope, id string, actor appaudit.Actor) error {
	if err := app.RequireScope(scope); err != nil {
		return err
	}
	if scope.Kind() != corescope.KindMaster {
		return errs.New(errs.CodeForbidden, "master role is required")
	}
	if err := ensureUserID(id); err != nil {
		return err
	}
	if id == actor.UserID {
		return errs.New(errs.CodeForbidden, "deleting your own account is not allowed here")
	}
	return s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		now := s.deps.Clock.Now().UTC()
		// GetByID 只回未删行：已删与不存在同判 404。
		target, err := s.deps.Users.GetByID(ctx, id)
		if err != nil {
			return err
		}
		owned, err := s.deps.OwnerAccounts.CountLiveAccountsByOwner(ctx, target.ID)
		if err != nil {
			return err
		}
		if owned > 0 {
			return errs.New(errs.CodeConflict, "user still owns live accounts; transfer or delete them first")
		}
		if err := s.deps.Users.SoftDelete(ctx, target.ID, actor.UserID, now); err != nil {
			return err
		}
		if err := s.deps.Sessions.RevokeAllForUser(ctx, target.ID); err != nil {
			return err
		}
		_, err = s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          now,
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionUserDelete,
			TargetType:  "user",
			TargetID:    target.ID,
			Detail:      map[string]any{"field": "deleted_at"},
		})
		return err
	})
}
