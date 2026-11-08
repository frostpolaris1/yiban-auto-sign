// 登录账号自助注销与宽限期恢复（lac2）。
//
// # 功能
// DeleteSelf 校验当前会话用户的登录口令后软删登录账号，并连带软删
// 名下全部生效的签到账号、注销全部会话；RestoreSelf 以邮箱＋登录口令
// 恢复 7 天宽限内的软删登录账号及其注销时刻生效的那一行签到账号。
// 本文件**不**做 HTTP 编码、**不**签发会话、**不**物理清除。
//
// # 归属
// app 层用例。端口见 repo.go 与 app/user.repo.go，实现见 adapters/pg。
// 入口层从 entry/http/router 调用；恢复是公开端点，注销要求会话。
//
// # 口径
// 注销先过 phc2 的二次口令门：缺 400、错 403、5 分钟内连败 5 次后
// 429 并带建议重试时长。master 账号一律 403：它是系统管理角色，
// 自助注销会把管理入口交出去；旧线对内置管理员同样直接拒绝。
// 注销与账号连带软删、会话撤销、审计同事务；detail 不含邮箱或口令。
// 恢复对「账号不存在 / 超窗 / 口令错 / 同邮箱已有活跃行」回答同一条
// 401，并在无真实哈希可比时执行等开销的 dummy 校验，拉平时延。
// 恢复成功不建立会话：旧线恢复即登录，新线把登录留给既有登录端点，
// 避免在公开恢复入口再签一套 cookie 与 sid。
// 会话撤销在 SoftDeleteSelf 之后、同一事务内完成。
package auth

import (
	"context"
	"fmt"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// ErrRestoreFailed 是自助恢复失败的唯一错误。
// 账号不存在、超出 7 天宽限、口令错误、同邮箱已有活跃行共用本值，
// 文案不区分原因，防批量探测「哪些邮箱近期注销过」。
var ErrRestoreFailed = errs.New(errs.CodeUnauthorized, "invalid email or password, or the restore window has ended")

// DeleteSelf 注销当前会话用户的登录账号。
//
// 先验证 current_password：空值 400、错误 403、节流 429。
// master 账号 403：与旧线内置管理员保护同口径，系统管理角色不能
// 通过自助面消失。软删用户与其名下全部生效账号，随后撤销全部会话，
// 最后写 user.self_delete 审计；任一步失败整体回滚。
func (s *Service) DeleteSelf(ctx context.Context, sess coreauth.Session, currentPassword string) error {
	if err := s.Reauthenticate(ctx, sess.User.ID, currentPassword); err != nil {
		return err
	}
	if sess.User.Role == coreauth.RoleMaster {
		return errs.New(errs.CodeForbidden, "master accounts cannot be deleted through self-service")
	}
	return s.tx.InTx(ctx, func(ctx context.Context) error {
		now := s.clock.Now().UTC()
		if _, err := s.users.SoftDeleteSelf(ctx, sess.User.ID, now); err != nil {
			return err
		}
		if err := s.sessions.RevokeAllForUser(ctx, sess.User.ID); err != nil {
			return err
		}
		actor := appaudit.NewActor(sess.User)
		_, err := s.audits.Append(ctx, coreaudit.Entry{
			TS:          now,
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionUserSelfDelete,
			TargetType:  "user",
			TargetID:    sess.User.ID,
			Detail:      map[string]any{"field": "deleted_at"},
		})
		return err
	})
}

// RestoreSelf 恢复 7 天宽限内软删的登录账号及其注销时刻生效的签到账号。
//
// 邮箱只用于定位最近一条软删行；活跃行不参与，故同邮箱已有活跃用户时
// 走与「不存在」相同的失败路径。口令校验在事务外完成，避免持行锁付 KDF；
// 事务内用 GetDeleted 重新锁定并复核宽限，再恢复用户与账号。
// 恢复成功不建立会话。
func (s *Service) RestoreSelf(ctx context.Context, email, password string) error {
	email = coreuser.NormalizeEmail(email)
	if !coreuser.ValidEmail(email) || password == "" {
		return errs.New(errs.CodeValidation, "email and password are required")
	}
	now := s.clock.Now().UTC()
	rec, err := s.users.FindDeletedByEmail(ctx, email)
	if err != nil {
		if isNotFound(err) {
			if padErr := s.verifyDummyPassword(ctx, password); padErr != nil {
				return padErr
			}
			return ErrRestoreFailed
		}
		return err
	}
	if rec.DeletedAt == nil || now.Sub(*rec.DeletedAt) > coreuser.SelfRestoreGrace {
		// 账号已超窗或行状态异常：仍付等开销的 dummy 校验，与口令错误同形。
		if padErr := s.verifyDummyPassword(ctx, password); padErr != nil {
			return padErr
		}
		return ErrRestoreFailed
	}
	check, err := s.kdf.Verify(ctx, password, rec.PasswordHash)
	if err != nil {
		// 坏哈希是数据事故，不伪装成口令错误。
		return fmt.Errorf("appauth: stored hash for %s is unreadable: %w", rec.ID, err)
	}
	if !check.Valid {
		return ErrRestoreFailed
	}
	err = s.tx.InTx(ctx, func(ctx context.Context) error {
		now := s.clock.Now().UTC()
		// GetDeleted 持 FOR UPDATE 到事务结束，与物理清除互斥；
		// 复核 deleted_at：KDF 期间行可能已被并发恢复（冲突）或清除（404）。
		locked, err := s.users.GetDeleted(ctx, rec.ID)
		if err != nil {
			code := errs.From(err).Code
			if code == errs.CodeNotFound || code == errs.CodeConflict {
				return ErrRestoreFailed
			}
			return err
		}
		if locked.DeletedAt == nil || now.Sub(*locked.DeletedAt) > coreuser.SelfRestoreGrace {
			return ErrRestoreFailed
		}
		// 只恢复本人注销的行：管理员删除是管理动作，不属于用户反悔范围。
		// 旧线管理员删除是硬删（行已不存在），本检查在新线软删模型下
		// 维持同一有效语义。
		if locked.DeletedBy != locked.ID {
			return ErrRestoreFailed
		}
		restored, err := s.users.RestoreSelfUser(ctx, locked.ID, now.Add(-coreuser.SelfRestoreGrace), s.userLimit)
		if err != nil {
			return err
		}
		// 注销时刻生效的那一行账号与用户行同一时间戳；管理员删除的行
		// deleted_by 不是本人，不会被本方法复活。
		if _, err := s.users.RestoreSelfAccount(ctx, restored.ID, *locked.DeletedAt, s.accountLimit); err != nil {
			return err
		}
		actor := appaudit.NewActor(coreauth.User{
			ID: restored.ID, Email: restored.Email, Role: restored.Role, Status: restored.Status,
		})
		_, err = s.audits.Append(ctx, coreaudit.Entry{
			TS:          now,
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionUserSelfRestore,
			TargetType:  "user",
			TargetID:    restored.ID,
			Detail:      map[string]any{"field": "deleted_at"},
		})
		return err
	})
	if err != nil {
		if errs.From(err).Code == errs.CodeConflict {
			return ErrRestoreFailed
		}
		return err
	}
	return nil
}
