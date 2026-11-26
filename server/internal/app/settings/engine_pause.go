// Package settings 内的全局执行暂停用例（批 3a）。
//
// # 功能
// Service.SetEnginePaused 写全局暂停开关（settings 行 engine.paused），
// Service.EnginePaused 读生效值。读走覆盖链解析器（DB>缺省，故障上抛），
// 写走覆盖行写端口（SetBool）。写入与 settings.engine_paused 审计同事务。
//
// # 归属
// app 层用例。覆盖行存取端口见 resolve.go，adapters/pg 实现；
// 入口层只做角色门与传输映射。
//
// # 口径
// 暂停与恢复都仅 master（不看请求目标值，也不看变化方向）：
// 两个方向对 admin 都 403，消除幂等重放歧义与「先停后复」绕过。
// 入口层先 Role 判，本用例层 RequireScope＋角色门双门，#341 形态。
// 缺行=false=不暂停（auto_approve 口径）；恢复写 false 不删行，
// 本用例无删行语义（ClearSetting 留给 sett2 的显式 null 补丁）。
// 幂等重放允许：重复写同一目标态仍落行、仍记一行审计——
// 审计记录的是操作，不是差量（sett2 同口径）。
// 审计 detail 只含字段与目标状态（paused/resumed），绝无口令；
// 操作者在审计行主体列（UserID/Role/Label），不进 detail。
// 审计失败整体回滚：覆盖行与审计行同存亡。
package settings

import (
	"context"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// auditTargetState 是审计 detail 的目标状态词。
// paused=本次请求把引擎暂停；resumed=本次请求解除暂停。
const (
	auditTargetPaused  = "paused"
	auditTargetResumed = "resumed"
)

// SetEnginePaused 写全局暂停开关。
// 校验在事务外完成：任何拒绝都零写入零审计。
// 写入与 settings.engine_paused 审计同事务；审计失败整体回滚。
func (s *Service) SetEnginePaused(ctx context.Context, scope corescope.Scope, actor appaudit.Actor, paused bool) error {
	if err := app.RequireScope(scope); err != nil {
		return err
	}
	// 暂停与恢复都仅 master（双向同一道门，不看请求目标值）。
	// 放在事务与存储之前：admin/user 零写入零审计。
	if scope.Kind() != corescope.KindMaster {
		return coreerrs.New(coreerrs.CodeForbidden, "master role is required")
	}
	if s.deps.Schedule == nil {
		// 装配缺写端口是编程错误：急停面必须显性，不得静默读成关闭。
		return coreerrs.New(coreerrs.CodeInternal, "settings override store is not configured")
	}
	target := auditTargetResumed
	if paused {
		target = auditTargetPaused
	}
	return s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		if err := s.deps.Schedule.SetBool(ctx, KeyEnginePaused, paused, actor.UserID); err != nil {
			return err
		}
		_, err := s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          s.deps.Clock.Now().UTC(),
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionSettingsEnginePaused,
			TargetType:  "settings",
			TargetID:    "engine_paused",
			// detail 只含字段与目标状态：field 形状不含点号，
			// 故键位写 engine_paused（对齐 auto_approve 的字段命名）。
			Detail: map[string]any{"field": "engine_paused", "to": target},
		})
		return err
	})
}

// EnginePaused 读全局暂停开关的生效值。
// 缺行=false；坏行 fail-closed（视为暂停）并出声一次；存储故障原样上抛——
// 故障不是「未暂停」，急停面不得因故障或坏值误放行。
// 读面不需要角色门：唯一的消费方（worker 步进与执行监视快照）
// 已在各自的入口层收窄到 master/admin。
func (s *Service) EnginePaused(ctx context.Context) (bool, error) {
	if s.deps.Resolver == nil {
		return false, coreerrs.New(coreerrs.CodeInternal, "settings resolver is not configured")
	}
	return s.deps.Resolver.ResolveEnginePaused(ctx)
}
