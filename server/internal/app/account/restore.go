// Package account 的软删恢复用例（8v8④）。
//
// # 功能
// master/admin 恢复软删账号；容量判据与零行分类在适配器。
//
// # 归属
// app 层用例。端口 Accounts.Restore 已有，适配器已实现，
// 本用例只管角色门槛、作用域与审计，不改适配器。
//
// # 口径
// 只允许 master 与 admin：管理员恢复不受 7 天窗口与 deleted_by 限制。
// 自助恢复（deleted_by=本人、7 天内、名下无其他生效账号）在 self.go。
// 容量在适配器的 advisory 锁下重数：全局上限与一人一号同建号判据。
// 已删行恢复 status 保持删除前原值；审计与恢复同事务。
package account

import (
	"context"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// Restore 恢复一个软删账号。
// 作用域外与不存在的账号 404；未删行 409（适配器口径）；
// 全局容量满或属主名下已有 live 账号 429（既有零行分类）。
// status 保持删除前原值。
func (s *Service) Restore(ctx context.Context, scope corescope.Scope, id string, actor appaudit.Actor) (Record, error) {
	if err := app.RequireScope(scope); err != nil {
		return Record{}, err
	}
	if scope.Kind() != corescope.KindMaster && scope.Kind() != corescope.KindAdmin {
		return Record{}, errs.New(errs.CodeForbidden, "role cannot restore accounts")
	}
	var record Record
	err := s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		var err error
		record, err = s.deps.Accounts.Restore(ctx, scope, id, s.deps.Limit)
		if err != nil {
			return err
		}
		_, err = s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          s.deps.Clock.Now().UTC(),
			SchoolID:    record.SchoolID,
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionAccountRestore,
			TargetType:  "account",
			TargetID:    record.ID,
			Detail:      map[string]any{"field": "deleted_at"},
		})
		return err
	})
	if err != nil {
		return Record{}, err
	}
	s.attachPhoneModel(ctx, scope, &record)
	return record, nil
}
