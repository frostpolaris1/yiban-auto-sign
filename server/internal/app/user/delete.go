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
// 目标是最后一名 active 未删 master 409（P2-3）：计数端口先取
// UserCapacityLock 再数，与 Update 的「锁→数→改」串行化；
// 判据只计 active（P1-2），停用 master 不能登录、不构成恢复路径。
// 事务内＝GetByID + 计数 + SoftDelete + RevokeAllForUser + 审计。
// 解析层 JOIN 已判 deleted_at；显式撤销让 sessions.revoked_at 有据可查。

package user

import (
	"context"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
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
	normalized, err := ensureUserID(id)
	if err != nil {
		return err
	}
	if normalized == actor.UserID {
		return errs.New(errs.CodeForbidden, "deleting your own account is not allowed here")
	}
	return s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		now := s.deps.Clock.Now().UTC()
		// GetByID 只回未删行：已删与不存在同判 404。用归一化 id。
		target, err := s.deps.Users.GetByID(ctx, normalized)
		if err != nil {
			return err
		}
		// 删除也参与 master 计数（独立审查 P2-3）：
		// 计数端口先取 UserCapacityLock 再数（适配器保证），
		// 与 Update 的「锁→数→改」串行化，堵「A 降 B 同时 B 删 A」的并发缝。
		// 判据只计 active 未删 master（P1-2）：删掉最后一名 active master 即 409。
		if target.Role == coreauth.RoleMaster {
			live, err := s.deps.Users.CountLiveMasters(ctx, target.ID)
			if err != nil {
				return err
			}
			if live == 0 {
				return errs.New(errs.CodeConflict, "the last active master cannot be deleted")
			}
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
