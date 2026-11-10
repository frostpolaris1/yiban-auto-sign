// Package account 的本人自助用例（5mx）。
//
// # 功能
// 已登录用户注销或撤销注销**自己名下**的账号：软删、7 天宽限与
// 自助恢复判据；角色不限（master/admin/user 都只能动自己的行）。
// 本包**不**做物理清除、**不**发邮件。
//
// # 归属
// app 层用例。端口见 repo.go，实现见 adapters/pg。
// 入口层从 entry/http/account 调用；作用域由会话 user id 构造。
//
// # 口径
// 归属只认 session user id：入口层传 corescope.User(会话用户)，
// 本层拒绝任何非 user 作用域，故 master 的全量作用域不会漏进来。
// 越权（别人的账号）一律 404，不暴露存在性。
// 已删除再删 400；管理员删除的账号用户恢复 403；
// 超过 coreaccount.SelfRestoreGrace（7 天）恢复 409；
// 名下已有生效账号恢复 400；不存在 404。
// 写动作与审计同事务；audit 追加是事务最后一条语句。
package account

import (
	"context"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// SelfDelete 注销调用者自己的账号：软删，deleted_by 记本人。
// 已删除再删 400；不存在或不属于本人 404。
func (s *Service) SelfDelete(ctx context.Context, scope corescope.Scope, id string, actor appaudit.Actor) error {
	if err := requireSelfScope(scope, actor); err != nil {
		return err
	}
	return s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		now := s.deps.Clock.Now().UTC()
		record, err := s.deps.Accounts.GetIncludingDeleted(ctx, scope, id)
		if err != nil {
			return err
		}
		if record.DeletedAt != nil {
			return errs.New(errs.CodeValidation, "account is already deleted")
		}
		if err := s.deps.Accounts.SoftDelete(ctx, scope, id, actor.UserID, now); err != nil {
			// 并发软删：条件 UPDATE 零行后重读一次，区分「刚被删」与「行不存在」。
			if errs.From(err).Code == errs.CodeNotFound {
				if current, readErr := s.deps.Accounts.GetIncludingDeleted(ctx, scope, id); readErr == nil && current.DeletedAt != nil {
					return errs.New(errs.CodeValidation, "account is already deleted")
				}
			}
			return err
		}
		_, err = s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          now,
			SchoolID:    record.SchoolID,
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionAccountSelfDelete,
			TargetType:  "account",
			TargetID:    record.ID,
			Detail:      map[string]any{"field": "deleted_at"},
		})
		return err
	})
}

// SelfRestore 撤销本人自删。判据全在仓储的条件 UPDATE 里：
// deleted_by=本人、deleted_at 不早于 now-SelfRestoreGrace、
// 名下无其他生效账号、容量足够。
// 管理员删除的账号 403；超窗或未删 409；名下已有生效账号 400；
// 不存在或不属于本人 404。
func (s *Service) SelfRestore(ctx context.Context, scope corescope.Scope, id string, actor appaudit.Actor) (Record, error) {
	if err := requireSelfScope(scope, actor); err != nil {
		return Record{}, err
	}
	// 配额在开事务前解析：读故障直接上抛，不当作不限。
	limit, err := s.deps.Quota.AccountLimit(ctx)
	if err != nil {
		return Record{}, err
	}
	var record Record
	err = s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		now := s.deps.Clock.Now().UTC()
		var err error
		record, err = s.deps.Accounts.RestoreSelf(ctx, scope, id, actor.UserID,
			now.Add(-coreaccount.SelfRestoreGrace), limit)
		if err != nil {
			return err
		}
		_, err = s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          now,
			SchoolID:    record.SchoolID,
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionAccountSelfRestore,
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

// requireSelfScope 只接受调用者本人的 user 作用域。
// 非 user 作用域（含 master 全量）一律 403：自助动作不得借道管理作用域。
func requireSelfScope(scope corescope.Scope, actor appaudit.Actor) error {
	if err := app.RequireScope(scope); err != nil {
		return err
	}
	if scope.Kind() != corescope.KindUser || actor.UserID == "" || actor.UserID != scope.UserID() {
		return errs.New(errs.CodeForbidden, "self-service actions require the caller's own scope")
	}
	return nil
}
