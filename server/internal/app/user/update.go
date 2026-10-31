// 用户更新用例：改角色与改状态，含最后 master 判据。
//
// # 功能
// 只实现 PATCH /api/users/{id} 对应的更新动作：校验输入、
// 事务内判最后 master、写行、写审计。本包**不**做 HTTP 编码、
// **不**撤销会话（状态变更的会话失效由解析层 JOIN 保证）。
//
// # 归属
// app 层用例。端口见 repo.go，实现见 adapters/pg。
// ensureUserID 供同包四个带 id 的用例共用。
//
// # 口径
// 只有 master 可以改用户（契约 x-roles）。其余角色一律 403。
// 目标不存在或已软删一律 404；id 不是 UUID 也报 404：
// PATCH /api/users/lookup 沿 {id} 路由进来时按「目标不存在」回，
// 不按形状回 400（lookup 静态段遮蔽判据，e2e 钉一次）。
// 最后 master 判据只数 active 未删 master（role='master' AND status='active'
// AND deleted_at IS NULL）。停用 master 不能登录（app/auth/login.go 要求
// active），不构成恢复路径；若计入停用行，A 停用 B 后 A 自降即可达
// 零 active master 锁死（独立审查 P1-2）。
// 计数在事务内 UserCapacityLock 下进行（端口注释），与建号、恢复串行化。
// 两条审计相邻追加，中间不做任何工作（与 account.Update 同形状）。

package user

import (
	"context"

	"github.com/google/uuid"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// UpdateInput 是改角色的输入。
// 两个指针区分「没给」与「给了默认值」；两个都 nil 是校验失败。
type UpdateInput struct {
	Role   *coreauth.Role
	Status *coreauth.Status
}

// ensureUserID 归一化目标 id 并拒绝非 UUID 字面量。
// google/uuid 的 Parse 接受大写、花括号、urn:uuid: 前缀与 32 位无连字符形，
// 它们都能通过 Postgres 的 ::uuid 定型命中同一行；
// self 判据若用原始串比较，非规范形即可绕过（独立审查 P1-1）。
// 因此**返回小写规范形**：self 比较与全部仓储调用只用返回值。
// 非 UUID 字面量（lookup 静态段遮蔽）与不存在同判 404。
func ensureUserID(id string) (string, error) {
	parsed, err := uuid.Parse(id)
	if err != nil {
		return "", errs.New(errs.CodeNotFound, "user not found")
	}
	return parsed.String(), nil
}

// Update 改一个用户的角色或状态。**只有 master 可以调用**。
func (s *Service) Update(ctx context.Context, scope corescope.Scope, id string, in UpdateInput, actor appaudit.Actor) (Record, error) {
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
	if in.Role == nil && in.Status == nil {
		return Record{}, errs.New(errs.CodeValidation, "at least one of role or status is required")
	}
	if in.Role != nil && !in.Role.Valid() {
		return Record{}, errs.New(errs.CodeValidation, "role must be master, admin or user")
	}
	if in.Status != nil && !in.Status.Valid() {
		return Record{}, errs.New(errs.CodeValidation, "status must be active or disabled")
	}

	var record Record
	err = s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		now := s.deps.Clock.Now().UTC()
		target, err := s.deps.Users.GetByID(ctx, normalized)
		if err != nil {
			return err
		}
		demote := in.Role != nil && *in.Role != coreauth.RoleMaster
		disable := in.Status != nil && *in.Status == coreauth.StatusDisabled
		if target.Role == coreauth.RoleMaster && (demote || disable) {
			live, err := s.deps.Users.CountLiveMasters(ctx, target.ID)
			if err != nil {
				return err
			}
			if live == 0 {
				return errs.New(errs.CodeConflict, "the last active master cannot be demoted or disabled")
			}
		}
		roleChanged := in.Role != nil && *in.Role != target.Role
		statusChanged := in.Status != nil && *in.Status != target.Status
		if roleChanged {
			if err := s.deps.Users.UpdateRole(ctx, target.ID, *in.Role, now); err != nil {
				return err
			}
		}
		if statusChanged {
			if err := s.deps.Users.SetStatus(ctx, target.ID, *in.Status, now); err != nil {
				return err
			}
		}
		// 重读一次让响应带真实状态；重读之后只剩相邻的两条审计追加。
		// 仓储调用一律用归一化 id（P1-1 补漏：此处原用 raw id）。
		record, err = s.deps.Users.GetByID(ctx, normalized)
		if err != nil {
			return err
		}
		if roleChanged {
			if _, err := s.deps.Audit.Append(ctx, coreaudit.Entry{
				TS:          now,
				ActorUserID: actor.UserID,
				ActorRole:   actor.Role,
				ActorLabel:  actor.Label,
				Action:      coreaudit.ActionUserRoleChange,
				TargetType:  "user",
				TargetID:    target.ID,
				Detail: map[string]any{
					"field": "role",
					"from":  string(target.Role),
					"to":    string(*in.Role),
				},
			}); err != nil {
				return err
			}
		}
		if statusChanged {
			action := coreaudit.ActionUserDisable
			if *in.Status == coreauth.StatusActive {
				action = coreaudit.ActionUserEnable
			}
			_, err := s.deps.Audit.Append(ctx, coreaudit.Entry{
				TS:          now,
				ActorUserID: actor.UserID,
				ActorRole:   actor.Role,
				ActorLabel:  actor.Label,
				Action:      action,
				TargetType:  "user",
				TargetID:    target.ID,
				Detail: map[string]any{
					"field": "status",
					"from":  string(target.Status),
					"to":    string(*in.Status),
				},
			})
			return err
		}
		return nil
	})
	if err != nil {
		return Record{}, err
	}
	return record, nil
}
