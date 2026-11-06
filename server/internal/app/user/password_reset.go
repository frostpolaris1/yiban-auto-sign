// 管理员重置口令用例。
//
// # 功能
// 只实现 POST /api/users/{id}/password-reset 对应的动作：
// 生成一次性临时口令、写新哈希、撤销目标全部会话、写审计。
// 本包**不**做 HTTP 编码、**不**记录口令、**不**发通知。
//
// # 归属
// app 层用例。形状照自助改密五步同事务（app/auth/change_password.go），
// 差异只有两处：无需旧口令校验，撤销的是目标全部会话而非其余会话。
//
// # 口径
// 只有 master 可以重置。目标是自己 403：误触会把手持一次性口令的
// 操作者自己登出，丢失唯一的展示机会。
// 临时口令取 coreauth.NewToken（43 字符 base64url，可键入），
// 它是全仓唯一生成点（canonical.tsv token.new.func）。
// argon2id 哈希在事务外算（与 create.go 同口径），不占行锁与链锁。
// 事务内＝UpdatePassword + RevokeAllForUser + 审计；审计 detail 为空，
// temp_password 绝不进审计、日志或后续响应。
// 响应返回前事务已提交，旧会话的下一个请求即 401。

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

// tempPasswordAttempts 是临时口令满足目标角色策略的重生成上限。
// 43 字符 base64url 几乎必然命中三类；上限只为极端随机序列兜底。
const tempPasswordAttempts = 5

// PasswordReset 重置目标用户口令并撤销其全部会话。
// 返回的临时口令只此一次，调用方必须立即交给响应。
// **只有 master 可以调用**；目标是自己 403。
// 临时口令生成后按目标角色档断言强度策略，不满足则重新生成；
// 上限次内仍不满足回 internal（随机源故障形状）。
func (s *Service) PasswordReset(ctx context.Context, scope corescope.Scope, id string, actor appaudit.Actor) (string, error) {
	if err := app.RequireScope(scope); err != nil {
		return "", err
	}
	if scope.Kind() != corescope.KindMaster {
		return "", errs.New(errs.CodeForbidden, "master role is required")
	}
	normalized, err := ensureUserID(id)
	if err != nil {
		return "", err
	}
	if normalized == actor.UserID {
		return "", errs.New(errs.CodeForbidden, "resetting your own password is not allowed here")
	}
	// 目标角色先读一次：临时口令要按它断言策略。
	// 事务内还会重读一次作 404 判据，两次读之间的角色漂移不影响结果：
	// 重读行只用于写行与审计，策略断言的是生成时刻的目标档位。
	target, err := s.deps.Users.GetByID(ctx, normalized)
	if err != nil {
		return "", err
	}
	temp, err := tempPasswordForRole(target.Role)
	if err != nil {
		return "", err
	}
	// 哈希在事务外算：argon2id 约几十毫秒，不进事务、不持锁。
	hash, err := s.deps.KDF.Hash(ctx, temp)
	if err != nil {
		return "", err
	}
	now := s.deps.Clock.Now().UTC()
	err = s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		// 目标消失或已软删即 404：GetByID 只回未删行。用归一化 id。
		target, err := s.deps.Users.GetByID(ctx, normalized)
		if err != nil {
			return err
		}
		if err := s.deps.Users.UpdatePassword(ctx, target.ID, hash, true, now); err != nil {
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
			Action:      coreaudit.ActionUserPasswordReset,
			TargetType:  "user",
			TargetID:    target.ID,
		})
		return err
	})
	if err != nil {
		return "", err
	}
	return temp, nil
}

// tempPasswordForRole 生成满足目标角色档策略的一次性临时口令。
// 上限次内不满足即 internal：43 字符 base64url 命中三类是压倒性事件，
// 连续失败只说明随机源坏了。
func tempPasswordForRole(role coreauth.Role) (string, error) {
	for i := 0; i < tempPasswordAttempts; i++ {
		candidate, err := coreauth.NewToken()
		if err != nil {
			return "", err
		}
		if coreauth.PasswordPolicyErrorForRole(candidate, role) == "" {
			return candidate, nil
		}
	}
	return "", errs.New(errs.CodeInternal, "cannot generate a temporary password satisfying the policy")
}
