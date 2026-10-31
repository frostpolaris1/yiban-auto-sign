// 恢复软删用户用例。
//
// # 功能
// 只实现 POST /api/users/{id}/restore 对应的恢复动作：
// 容量与邮箱判据都在适配器的条件 UPDATE 里，本用例只管
// 作用域、审计与响应。本包**不**写 SQL、**不**发通知。
//
// # 归属
// app 层用例。端口见 repo.go，实现见 adapters/pg（形状照账号 Restore）。
//
// # 口径
// 只有 master 可以恢复。未删与不存在一律 404；恢复后角色、状态
// 与邮箱保持删除前原值。撞 uq_users_email_live（活跃用户邮箱部分
// 唯一索引）由 mapError 报 409，不静默改名；活跃用户数达上限报 429。
// 会话已在删除时全部撤销，恢复后须重新登录。

package user

import (
	"context"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// Restore 恢复一个软删用户。**只有 master 可以调用**。
func (s *Service) Restore(ctx context.Context, scope corescope.Scope, id string, actor appaudit.Actor) (Record, error) {
	if err := app.RequireScope(scope); err != nil {
		return Record{}, err
	}
	if scope.Kind() != corescope.KindMaster {
		return Record{}, errs.New(errs.CodeForbidden, "master role is required")
	}
	normalized, err := ensureUserID(id)
	if err != nil {
		return Record{}, err
	}
	var record Record
	err = s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		now := s.deps.Clock.Now().UTC()
		var err error
		record, err = s.deps.Users.Restore(ctx, normalized, s.deps.Limit)
		if err != nil {
			return err
		}
		_, err = s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          now,
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionUserRestore,
			TargetType:  "user",
			TargetID:    record.ID,
			Detail:      map[string]any{"field": "deleted_at"},
		})
		return err
	})
	if err != nil {
		return Record{}, err
	}
	return record, nil
}
