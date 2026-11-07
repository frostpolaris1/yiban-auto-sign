// 物理清除已软删用户的用例。
//
// # 功能
// 只实现 POST /api/users/{id}/purge 对应的动作：
// 校验软删状态、物理删除用户行与名下账号及从属行、写审计。
// 本包**不**写 SQL、**不**做 HTTP 编码、**不**删任何窗口覆盖。
//
// # 归属
// app 层用例。端口见 repo.go（GetDeleted、Purge），实现见 adapters/pg。
//
// # 口径
// 只有 master 可以清除，其余 403。目标是操作者自己 403：
// 已软删用户不可能是当前会话（软删即撤销全部会话），
// 这是防御性判据，不是可达路径。
// 只允许已软删的用户：活行 409（适配器在锁下分类），不存在 404。
// 判定持行锁进行，与恢复互斥；GetByID 排除软删行，这里**不得**用它。
// 名下账号与从属行的删除顺序钉在外键上：任务行先分块删（RESTRICT），
// 被该用户审核的他人账号成对清 reviewed_at/reviewed_by，
// account_location.updated_by 置空；window_overrides 保留且
// created_by 随外键 SET NULL 置空（覆盖按日生效，不随创建者清除）。
// 审计行不随对象删除；user.purge 与物理删除同事务，
// detail 恒为空对象，不含邮箱。失败整事务回滚：零写入、零审计。

package user

import (
	"context"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// Purge 物理删除一个已软删用户。**只有 master 可以调用**。
func (s *Service) Purge(ctx context.Context, scope corescope.Scope, id string, actor appaudit.Actor) error {
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
		return errs.New(errs.CodeForbidden, "purging your own account is not allowed here")
	}
	return s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		// 锁行并校验软删状态；活行 conflict，不存在 not_found。
		// 行锁持有到事务结束：并发恢复在本事务提交前只能等待。
		target, err := s.deps.Users.GetDeleted(ctx, normalized)
		if err != nil {
			return err
		}
		if err := s.deps.Users.Purge(ctx, target.ID); err != nil {
			return err
		}
		_, err = s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          s.deps.Clock.Now().UTC(),
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionUserPurge,
			TargetType:  "user",
			TargetID:    target.ID,
			Detail:      map[string]any{},
		})
		return err
	})
}
