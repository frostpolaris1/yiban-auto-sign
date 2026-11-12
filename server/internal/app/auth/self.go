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
// 恢复对「账号不存在 / 超窗 / 口令错」回答同一条 401，并在无真实
// 哈希可比时执行等开销的 dummy 校验，拉平时延；口令已验对后才暴露的
// 撞号（注销期间邮箱被顶号创建、手机号被抢注）回答 409 与可懂文案——
// 折叠成 401 会把「号被抢」说成「邮箱或密码错误」，用户无从补救；
// 文案只说明占用事实，不泄露占用者身份。
// 恢复成功不建立会话：旧线恢复即登录，新线把登录留给既有登录端点，
// 避免在公开恢复入口再签一套 cookie 与 sid。
// 会话撤销在 SoftDeleteSelf 之后、同一事务内完成。
// 注销成功返回注销时刻，入口层据此给出恢复窗（7 天，core/user.SelfRestoreGrace）。
package auth

import (
	"context"
	"fmt"
	"time"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// ErrRestoreFailed 是自助恢复失败的同形错误。
// 账号不存在、超出 7 天宽限、口令错误共用本值，文案不区分原因，
// 防批量探测「哪些邮箱近期注销过」。撞号不在其列：那是口令已验对
// 之后才成立的服务器侧事实，见 ErrRestoreConflict。
var ErrRestoreFailed = errs.New(errs.CodeUnauthorized, "invalid email or password, or the restore window has ended")

// ErrRestoreConflict 是撞号恢复的明确语义（clc）。
// 注销后邮箱被管理侧建号顶掉、或手机号被他人账号抢注时，恢复撞
// 活跃行唯一索引；本值给出 409 与用户可懂的处置指引。文案固定，
// **不含**占用者身份——恢复入口先验口令，占用者信息对本人本就可见，
// 但对错误口令请求永远到不了这条分支，泄露面为零。
var ErrRestoreConflict = errs.New(errs.CodeConflict,
	"the account's email or phone number is already in use by another account; cannot restore, please contact an administrator")

// DeleteSelf 注销当前会话用户的登录账号，返回注销时刻（UTC）。
//
// 先验证 current_password：空值 400、错误 403、节流 429。
// master 账号 403：与旧线内置管理员保护同口径，系统管理角色不能
// 通过自助面消失。软删用户与其名下全部生效账号，随后撤销全部会话，
// 最后写 user.self_delete 审计；任一步失败整体回滚。
// 返回的注销时刻供入口层计算恢复窗：恢复窗 = 注销时刻＋7 天
// （core/user.SelfRestoreGrace），与恢复判据同锚点、同常数，
// 注销响应与冷静期登录引导因此共用一个事实源。
func (s *Service) DeleteSelf(ctx context.Context, sess coreauth.Session, currentPassword string) (time.Time, error) {
	if err := s.Reauthenticate(ctx, sess.User.ID, currentPassword); err != nil {
		return time.Time{}, err
	}
	if sess.User.Role == coreauth.RoleMaster {
		return time.Time{}, errs.New(errs.CodeForbidden, "master accounts cannot be deleted through self-service")
	}
	var deletedAt time.Time
	err := s.tx.InTx(ctx, func(ctx context.Context) error {
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
		if err == nil {
			deletedAt = now
		}
		return err
	})
	if err != nil {
		return time.Time{}, err
	}
	return deletedAt, nil
}

// RestoreSelf 恢复 7 天宽限内软删的登录账号及其注销时刻生效的签到账号。
//
// 邮箱只用于定位最近一条软删行。口令校验在事务外完成，避免持行锁
// 付 KDF；事务内用 GetDeleted 重新锁定并复核宽限，再恢复用户与账号。
// 口令错、账号不存在、超窗、管理员删除走同一条 401；口令已验对后撞
// 活跃行唯一索引（邮箱被顶号创建、手机号被抢注）返回
// ErrRestoreConflict（409），不再折叠成同一条 401。恢复成功不建立会话。
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
	// 配额在开事务前解析：读故障直接上抛，不当作不限。
	userLimit, err := s.quota.UserLimit(ctx)
	if err != nil {
		return err
	}
	accountLimit, err := s.quota.AccountLimit(ctx)
	if err != nil {
		return err
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
		restored, err := s.users.RestoreSelfUser(ctx, locked.ID, now.Add(-coreuser.SelfRestoreGrace), userLimit)
		if err != nil {
			return err
		}
		// 注销时刻生效的那一行账号与用户行同一时间戳；管理员删除的行
		// deleted_by 不是本人，不会被本方法复活。
		if _, err := s.users.RestoreSelfAccount(ctx, restored.ID, *locked.DeletedAt, accountLimit); err != nil {
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
			// 撞活跃行唯一索引：邮箱被顶号创建，或注销释放的手机号被抢注。
			// 走到这里口令已验对，409 不构成存在性泄露；事务整体回滚，
			// 用户与账号仍是软删行。GetDeleted 的「行已被并发恢复」冲突
			// 在事务内层另行分类，仍走同形 401。
			return ErrRestoreConflict
		}
		return err
	}
	return nil
}
