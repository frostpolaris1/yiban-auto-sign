// Package account 的批量动作用例。
//
// # 功能
// 对 1–10 个账号执行同一动作：审核通过、审核驳回、暂停、恢复、软删、恢复。
// 逐条独立事务、逐条审计；单条失败只记进该条结果，不阻断后续。
//
// # 归属
// app 层用例。只编排既有单条用例，不写 SQL、不批量化库操作。
//
// # 口径
// 只允许 master 与 admin：整批请求先过角色门槛。
// 逐条作用域判定沿用各单条用例的实际码：跨校与不存在按 per-item 处理。
// reason 只传给驳回动作；其余动作忽略。
// 本用例不包大事务：一个大事务会让一条失败回滚全部已成功项。
package account

import (
	"context"
	"slices"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// 批量动作取值。与契约 AccountBatchAction 的六值同集；
// 同集判据在 entry/http/oapi 的契约对账门，不靠枚举豁免。
const (
	BatchReviewApprove = "review_approve"
	BatchReviewReject  = "review_reject"
	BatchPause         = "pause"
	BatchResume        = "resume"
	BatchDelete        = "delete"
	BatchRestore       = "restore"
)

// batchActions 是批量动作的全集：校验与派发都以此为准。
// 新增动作时同改本表与 batchApply 的派发分支，漏一处即被守卫单测点名。
var batchActions = []string{
	BatchReviewApprove,
	BatchReviewReject,
	BatchPause,
	BatchResume,
	BatchDelete,
	BatchRestore,
}

// maxBatchItems 是一次批量请求的 id 上限。
const maxBatchItems = 10

// BatchItemResult 是一个 id 的处理结果。
// Code 为空即成功；失败时 Code 与 Message 来自 errs.From 的稳定错误。
type BatchItemResult struct {
	ID      string
	Code    errs.Code
	Message string
}

// OK 报告该条是否成功。
func (r BatchItemResult) OK() bool { return r.Code == "" }

// Batch 对 1–10 个账号执行同一动作。
// 返回顺序与入参 ids 一致；每个成功项走既有单条用例，审计语义不变。
// 动作未知、ids 为空、超限或重复是请求级错误，整批拒绝、零写入。
// 只允许 master 与 admin：暂停与软删的单条用例对属主放行，
// 批量面必须在整批入口收紧角色，否则契约的 master/admin 形状被绕过。
func (s *Service) Batch(ctx context.Context, scope corescope.Scope, action string, ids []string, reason string, actor appaudit.Actor) ([]BatchItemResult, error) {
	if err := app.RequireScope(scope); err != nil {
		return nil, err
	}
	if scope.Kind() != corescope.KindMaster && scope.Kind() != corescope.KindAdmin {
		return nil, errs.New(errs.CodeForbidden, "role cannot run account batch actions")
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
	seen := make(map[string]bool, len(ids))
	for _, id := range ids {
		if seen[id] {
			return nil, errs.New(errs.CodeValidation, "ids must not repeat an entry")
		}
		seen[id] = true
	}

	results := make([]BatchItemResult, 0, len(ids))
	for _, id := range ids {
		results = append(results, batchItemOf(id, s.batchApply(ctx, scope, action, id, reason, actor)))
	}
	return results, nil
}

// batchApply 把一个动作派发到既有单条用例。
// 与 batchActions 保持同集：全集里的每个动作都必须有派发分支。
// default 防御两处漂移：漏配派发分支的动作记 internal_error，绝不静默 ok。
func (s *Service) batchApply(ctx context.Context, scope corescope.Scope, action, id, reason string, actor appaudit.Actor) error {
	switch action {
	case BatchReviewApprove:
		_, err := s.Review(ctx, scope, id, true, "", actor)
		return err
	case BatchReviewReject:
		_, err := s.Review(ctx, scope, id, false, reason, actor)
		return err
	case BatchPause:
		_, err := s.Pause(ctx, scope, id, true, actor)
		return err
	case BatchResume:
		_, err := s.Pause(ctx, scope, id, false, actor)
		return err
	case BatchDelete:
		return s.Delete(ctx, scope, id, actor)
	case BatchRestore:
		_, err := s.Restore(ctx, scope, id, actor)
		return err
	default:
		return errs.New(errs.CodeInternal, "unknown batch action: "+action)
	}
}

// batchItemOf 把单条错误压成稳定码与用户安全消息。
// 未知错误归 internal_error，原始实现错误不进响应体。
func batchItemOf(id string, err error) BatchItemResult {
	if err == nil {
		return BatchItemResult{ID: id}
	}
	stable := errs.From(err)
	return BatchItemResult{ID: id, Code: stable.Code, Message: stable.Message}
}
