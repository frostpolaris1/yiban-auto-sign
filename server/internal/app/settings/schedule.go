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

// WriteOp 是一个设置键的写意图（调度与配额写面共用同一对语义）。
type WriteOp string

const (
	// OpSet 用给定值 upsert 覆盖行。
	OpSet WriteOp = "set"
	// OpClear 删除覆盖行，读面回退缺省。
	OpClear WriteOp = "clear"
)

// ScheduleOp 是调度写面的既有名字，保留为别名（语义单源在 WriteOp）。
type ScheduleOp = WriteOp

const (
	// ScheduleOpSet 用给定值 upsert 覆盖行。
	ScheduleOpSet = OpSet
	// ScheduleOpClear 删除覆盖行，读面回退 env。
	ScheduleOpClear = OpClear
)

// ScheduleSource 是调度设置生效值的来源词表（sett2 + 3b + 3c）。
// 名册三键（edge/allow_time_pref）可取 db/env/default；五枚 DB-only 键只取 db/default。
type ScheduleSource string

const (
	// ScheduleSourceDB 表示有效覆盖行（或完整默认窗口对）提供该键。
	ScheduleSourceDB ScheduleSource = "db"
	// ScheduleSourceEnv 表示进程环境提供该键（仅限名册三键）。
	ScheduleSourceEnv ScheduleSource = "env"
	// ScheduleSourceDefault 表示缺行或坏行，读内置缺省或配置名册缺省。
	ScheduleSourceDefault ScheduleSource = "default"
)

const (
	defaultEdgeFrontSec = 60
	defaultEdgeBackSec  = 60
)

// ScheduleEnvInt 是整型名册调度键的进程 env/registry 兜底快照。
type ScheduleEnvInt struct {
	Value  int
	Source ScheduleSource
}

// ScheduleEnvBool 是布尔名册调度键的进程 env/registry 兜底快照。
type ScheduleEnvBool struct {
	Value  bool
	Source ScheduleSource
}

// ScheduleEnv 保存名册 3 枚可写调度键的进程兜底视图，由 assembly 从 Config+Env 构造注入。
type ScheduleEnv struct {
	EdgeFrontSec  ScheduleEnvInt
	EdgeBackSec   ScheduleEnvInt
	AllowTimePref ScheduleEnvBool
}

// ScheduleIntValue 是生效整数调度设置及其来源。
type ScheduleIntValue struct {
	Value  int
	Source ScheduleSource
}

// ScheduleBoolValue 是生效布尔调度设置及其来源。
type ScheduleBoolValue struct {
	Value  bool
	Source ScheduleSource
}

// ScheduleSignDistValue 是生效分布档设置及其来源。
type ScheduleSignDistValue struct {
	Value  SignDist
	Source ScheduleSource
}

// ScheduleReport 汇总全部 8 枚可写调度键的生效值与来源（GET /api/admin/schedule-settings）。
type ScheduleReport struct {
	EdgeFrontSec        ScheduleIntValue
	EdgeBackSec         ScheduleIntValue
	AllowTimePref       ScheduleBoolValue
	SundaySign          ScheduleBoolValue
	SaturdaySign        ScheduleBoolValue
	DefaultSignStartMin ScheduleIntValue
	DefaultSignEndMin   ScheduleIntValue
	SignDist            ScheduleSignDistValue
}

// ScheduleChange 是一个调度设置键的写意图。
// Op=set 时 Value 是 int（edge/窗口键）、bool（布尔键）或
// SignDist（批 3c 分布档）；Op=clear 时 Value 必须为 nil。
type ScheduleChange struct {
	Key   string
	Op    ScheduleOp
	Value any
}

// ScheduleSettings 返回全部 8 枚可写调度键的生效值与来源。master 与 admin 可读。
// DB 覆盖行无效（坏行/越域/半对）时复用 Resolver 的单源回退与 Warn 语义；存储故障上抛 500。
func (s *Service) ScheduleSettings(ctx context.Context, scope corescope.Scope) (ScheduleReport, error) {
	if err := app.RequireScope(scope); err != nil {
		return ScheduleReport{}, err
	}
	if kind := scope.Kind(); kind != corescope.KindMaster && kind != corescope.KindAdmin {
		return ScheduleReport{}, coreerrs.New(coreerrs.CodeForbidden, "master or admin role is required")
	}
	if s.deps.Resolver == nil {
		return ScheduleReport{}, coreerrs.New(coreerrs.CodeInternal, "schedule settings resolver is not configured")
	}

	edgeFront, err := s.scheduleIntSetting(ctx, KeyScheduleEdgeFrontSec,
		normalizedIntFallback(s.deps.ScheduleEnv.EdgeFrontSec, defaultEdgeFrontSec))
	if err != nil {
		return ScheduleReport{}, err
	}
	edgeBack, err := s.scheduleIntSetting(ctx, KeyScheduleEdgeBackSec,
		normalizedIntFallback(s.deps.ScheduleEnv.EdgeBackSec, defaultEdgeBackSec))
	if err != nil {
		return ScheduleReport{}, err
	}
	allowPref, err := s.scheduleBoolWithEnvSetting(ctx, KeyScheduleAllowTimePref,
		normalizedBoolFallback(s.deps.ScheduleEnv.AllowTimePref, false))
	if err != nil {
		return ScheduleReport{}, err
	}
	sundayVal, sundaySrc, err := s.deps.Resolver.BoolSetting(ctx, KeyScheduleSundaySign, false)
	if err != nil {
		return ScheduleReport{}, err
	}
	saturdayVal, saturdaySrc, err := s.deps.Resolver.BoolSetting(ctx, KeyScheduleSaturdaySign, false)
	if err != nil {
		return ScheduleReport{}, err
	}
	winStart, winEnd, winSrc, err := s.deps.Resolver.SignWindowSetting(ctx,
		coreschedule.DefaultSignWindowStartMinute, coreschedule.DefaultSignWindowEndMinute)
	if err != nil {
		return ScheduleReport{}, err
	}
	signDist, distSrc, err := s.deps.Resolver.SignDistSetting(ctx, SignDistFront)
	if err != nil {
		return ScheduleReport{}, err
	}

	return ScheduleReport{
		EdgeFrontSec:        edgeFront,
		EdgeBackSec:         edgeBack,
		AllowTimePref:       allowPref,
		SundaySign:          ScheduleBoolValue{Value: sundayVal, Source: sundaySrc},
		SaturdaySign:        ScheduleBoolValue{Value: saturdayVal, Source: saturdaySrc},
		DefaultSignStartMin: ScheduleIntValue{Value: winStart, Source: winSrc},
		DefaultSignEndMin:   ScheduleIntValue{Value: winEnd, Source: winSrc},
		SignDist:            ScheduleSignDistValue{Value: signDist, Source: distSrc},
	}, nil
}

func (s *Service) scheduleIntSetting(ctx context.Context, key string, fallback ScheduleEnvInt) (ScheduleIntValue, error) {
	found, raw, err := s.deps.Resolver.Lookup(ctx, key)
	if err != nil {
		return ScheduleIntValue{}, err
	}
	if found {
		if value, ok := raw.(int); ok {
			return ScheduleIntValue{Value: value, Source: ScheduleSourceDB}, nil
		}
	}
	return ScheduleIntValue{Value: fallback.Value, Source: fallback.Source}, nil
}

func (s *Service) scheduleBoolWithEnvSetting(ctx context.Context, key string, fallback ScheduleEnvBool) (ScheduleBoolValue, error) {
	found, raw, err := s.deps.Resolver.Lookup(ctx, key)
	if err != nil {
		return ScheduleBoolValue{}, err
	}
	if found {
		if value, ok := raw.(bool); ok {
			return ScheduleBoolValue{Value: value, Source: ScheduleSourceDB}, nil
		}
	}
	return ScheduleBoolValue{Value: fallback.Value, Source: fallback.Source}, nil
}

func normalizedIntFallback(in ScheduleEnvInt, def int) ScheduleEnvInt {
	if in.Source == "" {
		return ScheduleEnvInt{Value: def, Source: ScheduleSourceDefault}
	}
	return in
}

func normalizedBoolFallback(in ScheduleEnvBool, def bool) ScheduleEnvBool {
	if in.Source == "" {
		return ScheduleEnvBool{Value: def, Source: ScheduleSourceDefault}
	}
	return in
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

// signWindowSideState 是窗口一侧「写后」的行形状。
type signWindowSideState int

const (
	signWindowAbsent  signWindowSideState = iota // 写后无行
	signWindowValid                              // 写后有有效行（值在域内）
	signWindowCorrupt                            // 写后残留坏行/越域行
)

// validateSignWindowPair 判默认窗口对的跨字段一致性（批 3b＋P2-16 外围）：
// 读面 ResolveSignWindow 的口径是「半对/坏行/越域 → 整对回退常量」，
// 写面同口径——**写后恰剩一侧有效行**的 PUT 收下的是永远不生效的死值，
// 一律 400：
//   - set 一侧而另一侧缺行或坏行 → 拒（本次写入造成死值；修复必须同一
//     PUT 成对 set）；
//   - 从完整对清掉一侧 → 拒（将另一侧翻成死值）；
//   - 存量半对/坏行的清理类写回**不**卡死：no-op clear（清本来就缺的一行）
//     与非 full 形状的清一侧保持放行（3b F4 口径），不涉窗口两键的普通
//     写回同样不校验。
//
// 写后两侧都是有效行时仍要求 start<end（原组合校验保持）。
// 生效值口径与读面一致：set 用新值；clear 后无行；未动时读 DB 行。
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
	preStart, startValue, err := s.signWindowRow(ctx, KeyScheduleDefaultSignStartMin)
	if err != nil {
		return err
	}
	preEnd, endValue, err := s.signWindowRow(ctx, KeyScheduleDefaultSignEndMin)
	if err != nil {
		return err
	}
	postStart, postStartValue := s.signWindowPostSide(preStart, startValue, startIntent)
	postEnd, postEndValue := s.signWindowPostSide(preEnd, endValue, endIntent)

	if postStart == signWindowValid && postEnd == signWindowValid {
		if postStartValue >= postEndValue {
			return coreerrs.New(coreerrs.CodeValidation,
				"default sign window must satisfy start < end after combining with the effective other side")
		}
		return nil
	}
	half := (postStart == signWindowValid) != (postEnd == signWindowValid)
	if !half {
		return nil
	}
	newlySet := (startIntent.op == ScheduleOpSet && postStart == signWindowValid) ||
		(endIntent.op == ScheduleOpSet && postEnd == signWindowValid)
	shrunkFromFull := preStart == signWindowValid && preEnd == signWindowValid
	if newlySet || shrunkFromFull {
		return coreerrs.New(coreerrs.CodeValidation,
			"default sign window keys must be written as a full pair (set both or clear both)")
	}
	return nil
}

// signWindowRow 读该侧当前行形状（写前）。越域行与坏行同为 corrupt；
// 缺行为 absent；存储故障原样上抛。
func (s *Service) signWindowRow(ctx context.Context, key string) (signWindowSideState, int, error) {
	row, found, err := s.deps.Schedule.IntOverride(ctx, key)
	if err != nil {
		if isSettingValueInvalid(err) {
			return signWindowCorrupt, 0, nil
		}
		return signWindowAbsent, 0, err
	}
	if !found {
		return signWindowAbsent, 0, nil
	}
	if row < signWindowMinuteMin || row > signWindowMinuteMax {
		return signWindowCorrupt, 0, nil
	}
	return signWindowValid, row, nil
}

// signWindowPostSide 计算写后该侧形状：set→valid(新值)；clear→absent；
// 未动→保持写前形状。
func (s *Service) signWindowPostSide(pre signWindowSideState, preValue int, intent signWindowIntent) (signWindowSideState, int) {
	switch intent.op {
	case ScheduleOpSet:
		return signWindowValid, intent.value
	case ScheduleOpClear:
		return signWindowAbsent, 0
	default:
		return pre, preValue
	}
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
// 布尔键（allow_time_pref 与两枚周末门）必须走 BoolOverride：
// IntOverride 读布尔行会按坏行返回错误，旧值被记成 nil。
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
	if key == KeyScheduleAllowTimePref || key == KeyScheduleSundaySign || key == KeyScheduleSaturdaySign {
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
