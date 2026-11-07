// 物理清除已软删账号的用例。
//
// # 功能
// 只实现 POST /api/accounts/{id}/purge 对应的动作：
// 校验软删状态、物理删除账号行与从属行、写审计。
// 本包**不**写 SQL、**不**做 HTTP 编码。
//
// # 归属
// app 层用例。端口见 repo.go（GetDeleted、Purge），实现见 adapters/pg。
//
// # 口径
// 只有 master 与 admin 可以清除；user 403。
// 只允许已软删的账号：活行 409（适配器在锁下分类），
// 不存在或作用域外 404。已删与活行的判定必须持行锁进行：
// 恢复方在锁上等待，不会出现「校验为已删后被并发恢复仍被物理删除」。
// Get 是排除软删行的读取面，这里**不得**用它。
// 从属行的删除顺序钉在外键上：任务行先分块删（RESTRICT），
// 凭据、出口、定位随外键级联。审计行不随对象删除；
// account.purge 与物理删除同事务，detail 恒为空对象，不含手机号。
// 失败整事务回滚：零写入、零审计。

package account

import (
	"context"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// Purge 物理删除一个已软删账号。**只有 master 与 admin 可以调用**。
func (s *Service) Purge(ctx context.Context, scope corescope.Scope, id string, actor appaudit.Actor) error {
	if err := app.RequireScope(scope); err != nil {
		return err
	}
	if scope.Kind() != corescope.KindMaster && scope.Kind() != corescope.KindAdmin {
		return errs.New(errs.CodeForbidden, "role cannot purge accounts")
	}
	return s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		// 锁行并校验软删状态；活行 conflict，不存在或越权 not_found。
		// 行锁持有到事务结束：并发恢复在本事务提交前只能等待。
		record, err := s.deps.Accounts.GetDeleted(ctx, scope, id)
		if err != nil {
			return err
		}
		if err := s.deps.Accounts.Purge(ctx, record.ID); err != nil {
			return err
		}
		_, err = s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          s.deps.Clock.Now().UTC(),
			SchoolID:    record.SchoolID,
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionAccountPurge,
			TargetType:  "account",
			TargetID:    record.ID,
			Detail:      map[string]any{},
		})
		return err
	})
}
