// Package account 的平台姓名揭示审计用例。
//
// # 功能
// 追加 account.person_name_reveal 审计行。揭示本身零业务写库：
// 姓名只在入口层的响应里出现一次，服务端不留存。
//
// # 归属
// app 层用例。入口层（entry/http/signing 的 PersonName）在平台认证
// 成功后调用；追加成功才把姓名写进响应，追加失败响应 5xx。
//
// # 口径
// detail 只带 field=person_name（EncodeDetail 白名单键，字段名非值），
// **绝无姓名本体**；TargetID 即账号 id。Append 是短事务的唯一语句。
package account

import (
	"context"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// RecordPersonNameReveal 记录一次平台姓名揭示。
// schoolID 由调用方从揭示用的凭据读数带入，本方法不再解密重读。
// Append 要求短事务（链 advisory 锁是事务级的）：本事务只有这一条语句。
func (s *Service) RecordPersonNameReveal(ctx context.Context, scope corescope.Scope, id, schoolID string, actor appaudit.Actor) error {
	if err := app.RequireScope(scope); err != nil {
		return err
	}
	return s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		_, err := s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          s.deps.Clock.Now().UTC(),
			SchoolID:    schoolID,
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionAccountPersonNameReveal,
			TargetType:  "account",
			TargetID:    id,
			Detail:      map[string]any{"field": "person_name"},
		})
		return err
	})
}
