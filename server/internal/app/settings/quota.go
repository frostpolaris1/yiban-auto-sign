// Package settings 内的配额写回用例（批 r4r）。
//
// # 功能
// Service.SetQuota 部分补丁写回两枚配额键：值变更＝upsert 覆盖行；
// 显式删除＝删行回缺省（1/1）。Service.QuotaSettings 回显两键的
// 生效值与来源（db/default）。
// 域校验：整数 [0,100000]，0=不限哨兵；越域、未知键与空更新一律拒绝，
// 零写入零审计。写入与 settings.quota_writeback 审计同事务；
// detail 只含变更键与旧→新值（整数不敏感，可明文）。
//
// # 归属
// app 层用例。覆盖行存取端口见 resolve.go，adapters/pg 实现；
// 入口层只做角色门与传输映射。
//
// # 口径
// 只有 master 可以写；master 与 admin 可以读（与名册读门一致）。
// 旧值读自覆盖行当前状态：行缺失或坏值记 nil（回退缺省的现状）。
// 幂等重放（旧==新）仍记一行审计：审计记录的是操作，不是差量。
// 审计失败整体回滚：覆盖行与审计行同存亡。
// 写回热生效：消费方（注册/建号/恢复）每请求经 Resolver 读一次。
package settings

import (
	"context"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// QuotaChange 是一个配额键的写意图。形态与 ScheduleChange 一致：
// Op=set 时 Value 是 int；Op=clear 时 Value 必须为 nil。
type QuotaChange struct {
	Key   string
	Op    WriteOp
	Value any
}

// QuotaSource 是配额生效值的来源词表。
// 配额键无 env 对应位，故只有 db 与 default 两值（不复用名册 Source 的五值全集）；
// 契约枚举 QuotaSettingSource 与本词表同集（enum_gate_test 钉住）。
type QuotaSource string

const (
	// QuotaSourceDB 表示有效覆盖行提供该键。
	QuotaSourceDB QuotaSource = "db"
	// QuotaSourceDefault 表示缺行或坏行，读内置缺省（1）。
	QuotaSourceDefault QuotaSource = "default"
)

// QuotaValue 是一个配额键的生效值与来源。
type QuotaValue struct {
	Value  int
	Source QuotaSource
}

// QuotaReport 是两枚配额键的只读视图。
type QuotaReport struct {
	MaxUsers    QuotaValue
	MaxAccounts QuotaValue
}

// QuotaSettings 读两枚配额键的生效值与来源。master 与 admin 可读。
// 存储故障原样上抛，**不**当作缺省：故障不是配置状态。
func (s *Service) QuotaSettings(ctx context.Context, scope corescope.Scope) (QuotaReport, error) {
	if err := app.RequireScope(scope); err != nil {
		return QuotaReport{}, err
	}
	if kind := scope.Kind(); kind != corescope.KindMaster && kind != corescope.KindAdmin {
		return QuotaReport{}, coreerrs.New(coreerrs.CodeForbidden, "master or admin role is required")
	}
	if s.deps.Resolver == nil {
		return QuotaReport{}, coreerrs.New(coreerrs.CodeInternal, "quota settings resolver is not configured")
	}
	users, usersSource, err := s.deps.Resolver.QuotaSetting(ctx, KeyQuotaMaxUsers, DefaultQuotaMaxUsers)
	if err != nil {
		return QuotaReport{}, err
	}
	accounts, accountsSource, err := s.deps.Resolver.QuotaSetting(ctx, KeyQuotaMaxAccounts, DefaultQuotaMaxAccounts)
	if err != nil {
		return QuotaReport{}, err
	}
	return QuotaReport{
		MaxUsers:    QuotaValue{Value: users, Source: usersSource},
		MaxAccounts: QuotaValue{Value: accounts, Source: accountsSource},
	}, nil
}

// SetQuota 部分补丁写回配额两键。**只有 master 可以调用**。
// 校验在事务外完成：任何拒绝都零写入零审计。
// 写入与 settings.quota_writeback 审计同事务；审计失败整体回滚。
func (s *Service) SetQuota(ctx context.Context, scope corescope.Scope, actor appaudit.Actor, changes []QuotaChange) error {
	if err := app.RequireScope(scope); err != nil {
		return err
	}
	if scope.Kind() != corescope.KindMaster {
		return coreerrs.New(coreerrs.CodeForbidden, "master role is required")
	}
	if s.deps.Schedule == nil {
		return coreerrs.New(coreerrs.CodeInternal, "quota settings store is not configured")
	}
	if s.deps.Resolver == nil {
		return coreerrs.New(coreerrs.CodeInternal, "quota settings resolver is not configured")
	}
	if len(changes) == 0 {
		return coreerrs.New(coreerrs.CodeValidation, "quota settings update is empty")
	}
	if err := validateQuotaChanges(changes); err != nil {
		return err
	}
	return s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		detail := make(map[string]any, len(changes))
		pairs := make(map[string][2]any, len(changes))
		for _, change := range changes {
			old, new, err := s.applyQuotaChange(ctx, change, actor)
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
			Action:      coreaudit.ActionSettingsQuotaWriteback,
			TargetType:  "settings",
			TargetID:    "quota",
			Detail:      detail,
		})
		return err
	})
}

// validateQuotaChanges 在事务外做全部拒绝判定：
// 键必须在配额键集内、Op 合法、值类型与键匹配且在 [0,100000] 内。
func validateQuotaChanges(changes []QuotaChange) error {
	for _, change := range changes {
		if change.Key != KeyQuotaMaxUsers && change.Key != KeyQuotaMaxAccounts {
			return coreerrs.New(coreerrs.CodeValidation, "unknown quota setting key")
		}
		if change.Op != OpSet && change.Op != OpClear {
			return coreerrs.New(coreerrs.CodeValidation, "unknown quota settings operation")
		}
		if change.Op == OpClear {
			if change.Value != nil {
				return coreerrs.New(coreerrs.CodeValidation, "clearing a setting must not carry a value")
			}
			continue
		}
		value, ok := change.Value.(int)
		if !ok {
			return coreerrs.New(coreerrs.CodeValidation, "quota settings require an integer")
		}
		if value < QuotaMin || value > QuotaMax {
			return coreerrs.New(coreerrs.CodeValidation,
				"quota must be between 0 and 100000 (0 means unlimited)")
		}
	}
	return nil
}

// applyQuotaChange 落一行覆盖并返回审计用的旧→新值对。
// 旧值读自覆盖行当前状态（行缺失或坏值记 nil）；写失败原样上抛。
func (s *Service) applyQuotaChange(ctx context.Context, change QuotaChange, actor appaudit.Actor) (any, any, error) {
	old, err := s.readQuotaOld(ctx, change.Key)
	if err != nil {
		return nil, nil, err
	}
	if change.Op == OpClear {
		if err := s.deps.Schedule.ClearSetting(ctx, change.Key); err != nil {
			return nil, nil, err
		}
		return old, nil, nil
	}
	value, ok := change.Value.(int)
	if !ok {
		// validateQuotaChanges 已挡住其他类型；此分支不可达。
		return nil, nil, coreerrs.New(coreerrs.CodeInternal, "unreachable quota change value")
	}
	if err := s.deps.Schedule.SetInt(ctx, change.Key, value, actor.UserID); err != nil {
		return nil, nil, err
	}
	return old, value, nil
}

// readQuotaOld 读覆盖行旧值作审计标量。
// 存储故障原样上抛；坏行（非整数、越域）按未覆盖（nil）——
// 坏值原文绝不进审计。
func (s *Service) readQuotaOld(ctx context.Context, key string) (any, error) {
	value, found, err := s.deps.Schedule.IntOverride(ctx, key)
	if err != nil {
		if isSettingValueInvalid(err) {
			return nil, nil
		}
		return nil, err
	}
	if !found || value < QuotaMin || value > QuotaMax {
		return nil, nil
	}
	return value, nil
}
