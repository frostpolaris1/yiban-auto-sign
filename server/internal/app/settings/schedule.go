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
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
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
// Op=set 时 Value 是 int（edge/窗口键）、bool（布尔键）或
// SignDist（批 3c 分布档）；Op=clear 时 Value 必须为 nil。
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
	if err := s.validateScheduleChanges(ctx, changes); err != nil {
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
// 键必须在写回集内、Op 合法、值类型与键匹配、整型值在名册域内，
// 默认窗口对再做跨字段一致性校验（批 3b）。
func (s *Service) validateScheduleChanges(ctx context.Context, changes []ScheduleChange) error {
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
		case KeyScheduleAllowTimePref, KeyScheduleSundaySign, KeyScheduleSaturdaySign:
			// 周末门两键（批 3b）与 allow_time_pref 同形：布尔字面，无 registry 域。
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
				return coreerrs.New(coreerrs.CodeValidation, "sign gate settings require a boolean")
			}
		case KeyScheduleSignDist:
			// 铺点分布档（批 3c）：只收强类型枚举且值在小写词表内；
			// 裸 string 也不收——入口层必须先经 ParseSignDist，防止绕过词表。
			if change.Op != ScheduleOpSet && change.Op != ScheduleOpClear {
				return coreerrs.New(coreerrs.CodeValidation, "unknown schedule settings operation")
			}
			if change.Op == ScheduleOpClear {
				if change.Value != nil {
					return coreerrs.New(coreerrs.CodeValidation, "clearing a setting must not carry a value")
				}
				continue
			}
			value, ok := change.Value.(SignDist)
			if !ok {
				return coreerrs.New(coreerrs.CodeValidation, "sign distribution settings require the sign_dist enum")
			}
			if _, ok := ParseSignDist(string(value)); !ok {
				return coreerrs.New(coreerrs.CodeValidation, "sign_dist must be front or uniform")
			}
		case KeyScheduleDefaultSignStartMin, KeyScheduleDefaultSignEndMin:
			// 默认窗口两键（批 3b）：分钟域不走 Resolver.CheckInt——
			// CheckInt 绑定 registry 注入域，而这两键是 DB-only、不在名册，
			// 没有名册域可注入；按派单把域写在用例层（与读面 ResolveSignWindow
			// 共用 resolve.go 的同一对域常量，不复制第二份）。
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
				return coreerrs.New(coreerrs.CodeValidation, "sign window settings require an integer")
			}
			if value < signWindowMinuteMin || value > signWindowMinuteMax {
				return coreerrs.New(coreerrs.CodeValidation,
					"sign window minute must be between 0 and 1439")
			}
		default:
			return coreerrs.New(coreerrs.CodeValidation, "unknown schedule setting key")
		}
	}
	return s.validateSignWindowPair(ctx, changes)
}

// signWindowIntent 收集本次 PUT 对默认窗口两键的写意图：
// 未列出（零值 Op）、set（新值）或 clear。
type signWindowIntent struct {
	op    ScheduleOp
	value int
}

// validateSignWindowPair 判默认窗口对的跨字段一致性（批 3b）：
// 本次 PUT 只改一侧时，与另一侧的生效值组合后仍需 start<end。
// 生效值口径与读面一致：本次 set 用新值；本次 clear 或未动时读 DB 行，
// 行缺失、坏行或越域一律按 core 常量（读面 ResolveSignWindow 同口径，
// 半对整对回退，不与行值拼接）。本次 PUT 不涉窗口两键时不校验：
// 半对的存量行不得卡死其他键的普通写回。
// 存储故障原样上抛（校验期读失败不静默写）。
func (s *Service) validateSignWindowPair(ctx context.Context, changes []ScheduleChange) error {
	var startIntent, endIntent signWindowIntent
	touched := false
	for _, change := range changes {
		switch change.Key {
		case KeyScheduleDefaultSignStartMin:
			startIntent.op, startIntent.value = change.Op, intValueOf(change.Value)
			touched = true
		case KeyScheduleDefaultSignEndMin:
			endIntent.op, endIntent.value = change.Op, intValueOf(change.Value)
			touched = true
		}
	}
	if !touched {
		return nil
	}
	start, err := s.signWindowEffectiveSide(ctx, KeyScheduleDefaultSignStartMin,
		startIntent, coreschedule.DefaultSignWindowStartMinute)
	if err != nil {
		return err
	}
	end, err := s.signWindowEffectiveSide(ctx, KeyScheduleDefaultSignEndMin,
		endIntent, coreschedule.DefaultSignWindowEndMinute)
	if err != nil {
		return err
	}
	if start >= end {
		return coreerrs.New(coreerrs.CodeValidation,
			"default sign window must satisfy start < end after combining with the effective other side")
	}
	return nil
}

// signWindowEffectiveSide 计算写后该侧的生效分钟值：
// set 用新值；clear 或未动时读覆盖行，行缺失/坏行/越域按常量。
func (s *Service) signWindowEffectiveSide(ctx context.Context, key string, intent signWindowIntent, fallback int) (int, error) {
	if intent.op == ScheduleOpSet {
		return intent.value, nil
	}
	if intent.op == ScheduleOpClear {
		return fallback, nil
	}
	row, found, err := s.deps.Schedule.IntOverride(ctx, key)
	if err != nil {
		if isSettingValueInvalid(err) {
			return fallback, nil
		}
		return 0, err
	}
	if !found || row < signWindowMinuteMin || row > signWindowMinuteMax {
		return fallback, nil
	}
	return row, nil
}

// intValueOf 取 set 意图里的整型值；clear/未动时零值。
// 值类型已由 validateScheduleChanges 的逐键分支判定，这里只取数。
func intValueOf(value any) int {
	v, _ := value.(int)
	return v
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
	case SignDist:
		if err := s.deps.Schedule.SetString(ctx, change.Key, string(value), actor.UserID); err != nil {
			return nil, nil, err
		}
		return old, string(value), nil
	}
	// validateScheduleChanges 已挡住其他类型；此分支不可达。
	return nil, nil, coreerrs.New(coreerrs.CodeInternal, "unreachable schedule change value")
}

// readOverrideOld 读覆盖行旧值作审计标量。
// 存储故障原样上抛；坏行按未覆盖（nil）——坏值原文绝不进审计。
func (s *Service) readOverrideOld(ctx context.Context, key string) (any, error) {
	if key == KeyScheduleSignDist {
		raw, found, err := s.deps.Schedule.StringOverride(ctx, key)
		if err != nil {
			if isSettingValueInvalid(err) {
				return nil, nil
			}
			return nil, err
		}
		if !found {
			return nil, nil
		}
		if _, ok := ParseSignDist(raw); !ok {
			return nil, nil
		}
		return raw, nil
	}
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
