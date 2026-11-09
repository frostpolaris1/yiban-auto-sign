// Package settings 内的调度设置写回用例（sett2 批）。
//
// # 功能
// Service.SetSchedule 部分补丁写回三枚调度设置键：
// 值变更＝upsert 覆盖行；显式删除＝删行回 env。
// 域校验唯一源是 resolver 持有的 registry 域（edge 整型 [0,300]，
// 布尔只认真小写 true/false 字面）；越域、未知键与空更新一律拒绝，
// 零写入零审计。写入与 settings.schedule_writeback 审计同事务；
// detail 只含变更键与旧→新值（int/bool 不敏感，可明文）。
//
// # 归属
// app 层用例。覆盖行存取端口见 resolve.go，adapters/pg 实现；
// 入口层只做角色门与传输映射。
//
// # 口径
// 只有 master 可以写（对齐 auto_approve 写门）；设置级写入不要求二次口令。
// 旧值读自覆盖行当前状态：行缺失记 nil（回退 env 的现状）。
// 幂等重放（旧==新）仍记一行审计：审计记录的是操作，不是差量。
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

// ScheduleOp 是一个键的写意图。
type ScheduleOp string

const (
	// ScheduleOpSet 用给定值 upsert 覆盖行。
	ScheduleOpSet ScheduleOp = "set"
	// ScheduleOpClear 删除覆盖行，读面回退 env。
	ScheduleOpClear ScheduleOp = "clear"
)

// ScheduleChange 是一个调度设置键的写意图。
// Op=set 时 Value 是 int（edge 键）或 bool（allow_time_pref）；
// Op=clear 时 Value 必须为 nil。
type ScheduleChange struct {
	Key   string
	Op    ScheduleOp
	Value any
}

// SetSchedule 部分补丁写回调度设置。**只有 master 可以调用**。
// 校验在事务外完成：任何拒绝都零写入零审计。
// 写入与 settings.schedule_writeback 审计同事务；审计失败整体回滚。
func (s *Service) SetSchedule(ctx context.Context, scope corescope.Scope, actor appaudit.Actor, changes []ScheduleChange) error {
	if err := app.RequireScope(scope); err != nil {
		return err
	}
	if scope.Kind() != corescope.KindMaster {
		return coreerrs.New(coreerrs.CodeForbidden, "master role is required")
	}
	if s.deps.Schedule == nil {
		return coreerrs.New(coreerrs.CodeInternal, "schedule settings store is not configured")
	}
	if s.deps.Resolver == nil {
		return coreerrs.New(coreerrs.CodeInternal, "schedule settings resolver is not configured")
	}
	if len(changes) == 0 {
		return coreerrs.New(coreerrs.CodeValidation, "schedule settings update is empty")
	}
	if err := s.validateScheduleChanges(changes); err != nil {
		return err
	}
	return s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		detail := make(map[string]any, len(changes))
		pairs := make(map[string][2]any, len(changes))
		for _, change := range changes {
			old, new, err := s.applyScheduleChange(ctx, change, actor)
			if err != nil {
				return err
			}
			pairs[change.Key] = [2]any{old, new}
		}
		detail["changes"] = pairs
		_, err := s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          s.deps.Clock.Now().UTC(),
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionSettingsScheduleWriteback,
			TargetType:  "settings",
			TargetID:    "schedule",
			Detail:      detail,
		})
		return err
	})
}

// validateScheduleChanges 在事务外做全部拒绝判定：
// 键必须在写回集内、Op 合法、值类型与键匹配、整型值在名册域内。
func (s *Service) validateScheduleChanges(changes []ScheduleChange) error {
	for _, change := range changes {
		switch change.Key {
		case KeyScheduleEdgeFrontSec, KeyScheduleEdgeBackSec:
			if change.Op != ScheduleOpSet && change.Op != ScheduleOpClear {
				return coreerrs.New(coreerrs.CodeValidation, "unknown schedule settings operation")
			}
			if change.Op == ScheduleOpClear {
				if change.Value != nil {
					return coreerrs.New(coreerrs.CodeValidation, "clearing a setting must not carry a value")
				}
				continue
			}
			value, ok := change.Value.(int)
			if !ok {
				return coreerrs.New(coreerrs.CodeValidation, "edge settings require an integer")
			}
			if err := s.deps.Resolver.CheckInt(change.Key, value); err != nil {
				return err
			}
		case KeyScheduleAllowTimePref:
			if change.Op != ScheduleOpSet && change.Op != ScheduleOpClear {
				return coreerrs.New(coreerrs.CodeValidation, "unknown schedule settings operation")
			}
			if change.Op == ScheduleOpClear {
				if change.Value != nil {
					return coreerrs.New(coreerrs.CodeValidation, "clearing a setting must not carry a value")
				}
				continue
			}
			if _, ok := change.Value.(bool); !ok {
				return coreerrs.New(coreerrs.CodeValidation, "allow_time_pref requires a boolean")
			}
		default:
			return coreerrs.New(coreerrs.CodeValidation, "unknown schedule setting key")
		}
	}
	return nil
}

// applyScheduleChange 落一行覆盖并返回审计用的旧→新值对。
// 旧值读自覆盖行当前状态（行缺失记 nil）；写失败原样上抛。
func (s *Service) applyScheduleChange(ctx context.Context, change ScheduleChange, actor appaudit.Actor) (any, any, error) {
	old, err := s.readOverrideOld(ctx, change.Key)
	if err != nil {
		return nil, nil, err
	}
	if change.Op == ScheduleOpClear {
		if err := s.deps.Schedule.ClearSetting(ctx, change.Key); err != nil {
			return nil, nil, err
		}
		return old, nil, nil
	}
	switch value := change.Value.(type) {
	case int:
		if err := s.deps.Schedule.SetInt(ctx, change.Key, value, actor.UserID); err != nil {
			return nil, nil, err
		}
		return old, value, nil
	case bool:
		if err := s.deps.Schedule.SetBool(ctx, change.Key, value, actor.UserID); err != nil {
			return nil, nil, err
		}
		return old, value, nil
	}
	// validateScheduleChanges 已挡住其他类型；此分支不可达。
	return nil, nil, coreerrs.New(coreerrs.CodeInternal, "unreachable schedule change value")
}

// readOverrideOld 读覆盖行旧值作审计标量。
// 存储故障原样上抛；坏行按未覆盖（nil）——坏值原文绝不进审计。
func (s *Service) readOverrideOld(ctx context.Context, key string) (any, error) {
	if key == KeyScheduleAllowTimePref {
		value, found, err := s.deps.Schedule.BoolOverride(ctx, key)
		if err != nil {
			if isSettingValueInvalid(err) {
				return nil, nil
			}
			return nil, err
		}
		if !found {
			return nil, nil
		}
		return value, nil
	}
	value, found, err := s.deps.Schedule.IntOverride(ctx, key)
	if err != nil {
		if isSettingValueInvalid(err) {
			return nil, nil
		}
		return nil, err
	}
	if !found {
		return nil, nil
	}
	return value, nil
}
