// 用户批量动作用例。
//
// # 功能
// 对 1–10 个用户执行同一动作：停用、启用、重置口令、软删、恢复。
// 逐条独立事务；单条失败只记进该条结果，不阻断后续。
// 审计与单条端点同口径：有变化写一行审计，幂等无变化（如已停用
// 再停用）不写。
//
// # 归属
// app 层用例。只编排既有单条用例，不写 SQL、不批量化库操作。
//
// # 口径
// 只允许 master：全部单条用户用例本就 master 专用，批量入口同门收紧，
// admin/user 整批 403。
// 最后 active master 保护、自删与自重置 403、恢复撞活跃邮箱 409
// 都沿用各单条用例的实际码，按 per-item 处理。
// 本用例不包大事务：一个大事务会让一条失败回滚全部已成功项。

package user

import (
	"context"
	"slices"

	"github.com/google/uuid"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// 批量动作取值。与契约 UserBatchAction 的五值同集；
// 同集判据在 entry/http/oapi 的契约对账门，不靠枚举豁免。
const (
	BatchDisable       = "disable"
	BatchEnable        = "enable"
	BatchResetPassword = "reset_password"
	BatchDelete        = "delete"
	BatchRestore       = "restore"
)

// batchActions 是批量动作的全集：校验与派发都以此为准。
// 新增动作时同改本表与 batchApply 的派发分支，漏一处即被守卫单测点名。
var batchActions = []string{
	BatchDisable,
	BatchEnable,
	BatchResetPassword,
	BatchDelete,
	BatchRestore,
}

// maxBatchItems 是一次批量请求的 id 上限。
const maxBatchItems = 10

// BatchItemResult 是一个 id 的处理结果。
// Code 为空即成功；失败时 Code 与 Message 来自 errs.From 的稳定错误。
// TempPassword 只在 reset_password 成功项携带，其余动作恒空。
type BatchItemResult struct {
	ID           string
	Code         errs.Code
	Message      string
	TempPassword string
}

// OK 报告该条是否成功。
func (r BatchItemResult) OK() bool { return r.Code == "" }

// Batch 对 1–10 个用户执行同一动作。
// 返回顺序与入参 ids 一致；每个成功项走既有单条用例，审计语义不变。
// 动作未知、ids 为空、超限或重复是请求级错误，整批拒绝、零写入。
// 只允许 master：admin/user 的单条用户用例本就 403，批量入口先拒，
// 否则整批请求会以零审计的形状穿过角色门槛。
func (s *Service) Batch(ctx context.Context, scope corescope.Scope, action string, ids []string, actor appaudit.Actor) ([]BatchItemResult, error) {
	if err := app.RequireScope(scope); err != nil {
		return nil, err
	}
	if scope.Kind() != corescope.KindMaster {
		return nil, errs.New(errs.CodeForbidden, "master role is required")
	}
	if !slices.Contains(batchActions, action) {
		return nil, errs.New(errs.CodeValidation, "unknown batch action")
	}
	if len(ids) == 0 {
		return nil, errs.New(errs.CodeValidation, "ids must not be empty")
	}
	if len(ids) > maxBatchItems {
		return nil, errs.New(errs.CodeValidation, "ids must hold at most 10 entries")
	}
	// 重复判据按 UUID 规范形归一化：同一用户的大小写、花括号、urn 形式
	// 两次入批即整批 400；非 UUID 字面量按原文比较。
	// 归一化只用于去重键，响应仍回显请求里的原始 id。
	seen := make(map[string]bool, len(ids))
	for _, id := range ids {
		key := id
		if parsed, err := uuid.Parse(id); err == nil {
			key = parsed.String()
		}
		if seen[key] {
			return nil, errs.New(errs.CodeValidation, "ids must not repeat an entry")
		}
		seen[key] = true
	}

	results := make([]BatchItemResult, 0, len(ids))
	for _, id := range ids {
		temp, err := s.batchApply(ctx, scope, action, id, actor)
		results = append(results, batchItemOf(id, temp, err))
	}
	return results, nil
}

// batchApply 把一个动作派发到既有单条用例。
// 返回的字符串只在 reset_password 成功时携带一次性临时口令。
// 与 batchActions 保持同集：全集里的每个动作都必须有派发分支。
// default 防御两处漂移：漏配派发分支的动作记 internal_error，绝不静默 ok。
func (s *Service) batchApply(ctx context.Context, scope corescope.Scope, action, id string, actor appaudit.Actor) (string, error) {
	switch action {
	case BatchDisable:
		status := coreauth.StatusDisabled
		_, err := s.Update(ctx, scope, id, UpdateInput{Status: &status}, actor)
		return "", err
	case BatchEnable:
		status := coreauth.StatusActive
		_, err := s.Update(ctx, scope, id, UpdateInput{Status: &status}, actor)
		return "", err
	case BatchResetPassword:
		return s.PasswordReset(ctx, scope, id, actor)
	case BatchDelete:
		return "", s.Delete(ctx, scope, id, actor)
	case BatchRestore:
		_, err := s.Restore(ctx, scope, id, actor)
		return "", err
	default:
		return "", errs.New(errs.CodeInternal, "unknown batch action: "+action)
	}
}

// batchItemOf 把单条错误压成稳定码与用户安全消息。
// 失败项绝不携带临时口令；成功且无口令的项该字段为空。
// 未知错误归 internal_error，原始实现错误不进响应体。
func batchItemOf(id, temp string, err error) BatchItemResult {
	if err == nil {
		return BatchItemResult{ID: id, TempPassword: temp}
	}
	stable := errs.From(err)
	return BatchItemResult{ID: id, Code: stable.Code, Message: stable.Message}
}
