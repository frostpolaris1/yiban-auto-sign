// 本人签到失败邮件开关用例（5mx）。
//
// # 功能
// 读、写当前用户的邮件开关；写入与 user.mail_notify 审计同事务。
// 投递（0v8）不在这里：本批只存取取值。
//
// # 归属
// app 层用例。端口见 repo.go（Users.MailNotify / SetMailNotify），
// 实现见 adapters/pg。入口层从 entry/http/user 调用。
//
// # 口径
// 只动调用者自己的行：userID 必须等于审计主体的 user id。
// 缺省 true（旧线 parity）；行缺失或已软删报 not_found。
// detail 只记字段与目标状态 on/off，**绝无邮箱原文**。
// 开关是用户**本人**的签到失败邮件总开关，不是管理员摘要订阅：
// 0v8 投递用户邮件要求 mail_notify=true；管理员每日摘要只看
// notification_prefs.sign_failed_daily。两者不得 OR，也不得互相替代。
package user

import (
	"context"
	"strings"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

// MailNotify 读调用者自己的邮件开关。
func (s *Service) MailNotify(ctx context.Context, userID string) (bool, error) {
	if strings.TrimSpace(userID) == "" {
		return false, errs.New(errs.CodeUnauthorized, "authentication required")
	}
	return s.deps.Users.MailNotify(ctx, userID)
}

// SetMailNotify 写调用者自己的邮件开关，并追加一条 user.mail_notify 审计。
// 目标用户必须就是审计主体；否则 403，防止拿别人的 id 改开关。
func (s *Service) SetMailNotify(ctx context.Context, userID string, enabled bool, actor appaudit.Actor) (bool, error) {
	if strings.TrimSpace(userID) == "" || userID != actor.UserID {
		return false, errs.New(errs.CodeForbidden, "user cannot change another user's mail switch")
	}
	target := "off"
	if enabled {
		target = "on"
	}
	err := s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		if err := s.deps.Users.SetMailNotify(ctx, userID, enabled); err != nil {
			return err
		}
		_, err := s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          s.deps.Clock.Now().UTC(),
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionUserMailNotify,
			TargetType:  "user",
			TargetID:    userID,
			Detail:      map[string]any{"field": "mail_notify", "to": target},
		})
		return err
	})
	if err != nil {
		return false, err
	}
	return enabled, nil
}
