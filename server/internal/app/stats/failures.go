package stats

// 失败归因聚合用例（任务 07）。
//
// # 功能
// 按角色范围与可选 school_id/task_id 读取某业务日终态失败的任务行分布；
// 空串 result 归入 unknown，词表外取值原样保留，按 count 降序、result 升序排列。
//
// # 口径
// master 全量；admin 只读绑定院校；跨校或不存在的 school_id/task_id 回 404
// （不泄露存在性）；同作用域内 school_id 与 task_id 不配套回 400。

import (
	"context"
	"slices"
	"sort"
	"strings"
	"time"

	"github.com/google/uuid"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coremask "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// FailureReport 是按业务日收敛的失败归因结果。
type FailureReport struct {
	Day      string
	SchoolID string
	TaskID   string
	Items    []coreschedule.SignFailureBucket
	Total    int
}

// Failures 按作用域与筛选条件返回某业务日终态失败的结果分布。
func (s *Service) Failures(ctx context.Context, scope corescope.Scope, filter coreschedule.SignFailureFilter) (FailureReport, error) {
	if err := requireStatsScope(scope); err != nil {
		return FailureReport{}, err
	}
	if err := normalizeAndValidateFailureFilter(&filter); err != nil {
		return FailureReport{}, err
	}
	if err := s.authorizeFailureScope(ctx, scope, filter); err != nil {
		return FailureReport{}, err
	}
	raw, err := s.store.FailureCounts(ctx, scope, filter)
	if err != nil {
		return FailureReport{}, err
	}
	items, total := normalizeFailureBuckets(raw)
	return FailureReport{
		Day:      filter.Day,
		SchoolID: filter.SchoolID,
		TaskID:   filter.TaskID,
		Items:    items,
		Total:    total,
	}, nil
}

func requireStatsScope(scope corescope.Scope) error {
	if err := app.RequireScope(scope); err != nil {
		return err
	}
	if scope.Kind() != corescope.KindMaster && scope.Kind() != corescope.KindAdmin {
		return coreerrs.New(coreerrs.CodeForbidden, "role cannot read sign statistics")
	}
	return nil
}

func normalizeAndValidateFailureFilter(filter *coreschedule.SignFailureFilter) error {
	if len(filter.Day) != len("2006-01-02") {
		return coreerrs.New(coreerrs.CodeValidation, "day must be YYYY-MM-DD")
	}
	parsedDay, err := time.Parse("2006-01-02", filter.Day)
	if err != nil || parsedDay.Year() < 1 {
		return coreerrs.New(coreerrs.CodeValidation, "day must be YYYY-MM-DD")
	}
	if filter.SchoolID != "" {
		parsed, err := uuid.Parse(filter.SchoolID)
		if err != nil {
			return coreerrs.New(coreerrs.CodeValidation, "school_id must be a valid UUID")
		}
		filter.SchoolID = parsed.String()
	}
	if filter.TaskID != "" {
		parsed, err := uuid.Parse(filter.TaskID)
		if err != nil {
			return coreerrs.New(coreerrs.CodeValidation, "task_id must be a valid UUID")
		}
		filter.TaskID = parsed.String()
	}
	return nil
}

// authorizeFailureScope 校验可选筛选的院校与任务作用域及归属关系。
// 跨校判定先于存在性与配套校验，防止通过 400/404 差异探测跨校 ID 存在性。
func (s *Service) authorizeFailureScope(ctx context.Context, scope corescope.Scope, filter coreschedule.SignFailureFilter) error {
	if filter.SchoolID != "" {
		if !scopeAllowsSchool(scope, filter.SchoolID) {
			return coreerrs.New(coreerrs.CodeNotFound, "school not found")
		}
		exists, err := s.store.SchoolExists(ctx, filter.SchoolID)
		if err != nil {
			return err
		}
		if !exists {
			return coreerrs.New(coreerrs.CodeNotFound, "school not found")
		}
	}
	if filter.TaskID != "" {
		taskSchoolID, err := s.store.TaskSchoolID(ctx, filter.TaskID)
		if err != nil {
			// 不存在与跨校必须回同一文案，避免 admin 凭 404 文案探测跨校 task_id 是否存在。
			if coreerrs.From(err).Code == coreerrs.CodeNotFound {
				return coreerrs.New(coreerrs.CodeNotFound, "school task not found")
			}
			return err
		}
		if !scopeAllowsSchool(scope, taskSchoolID) {
			return coreerrs.New(coreerrs.CodeNotFound, "school task not found")
		}
		if filter.SchoolID != "" && taskSchoolID != filter.SchoolID {
			return coreerrs.New(coreerrs.CodeValidation, "task does not belong to the school")
		}
	}
	return nil
}

func scopeAllowsSchool(scope corescope.Scope, schoolID string) bool {
	if scope.Kind() == corescope.KindMaster {
		return true
	}
	return scope.Kind() == corescope.KindAdmin && slices.Contains(scope.SchoolIDs(), schoolID)
}

func normalizeFailureBuckets(raw []coreschedule.SignFailureBucket) ([]coreschedule.SignFailureBucket, int) {
	byResult := make(map[string]int, len(raw))
	total := 0
	for _, b := range raw {
		key := b.Result
		if strings.TrimSpace(key) == "" {
			key = string(contract.OutcomeUnknown)
		} else {
			key = coremask.MaskText(key)
		}
		byResult[key] += b.Count
		total += b.Count
	}
	items := make([]coreschedule.SignFailureBucket, 0, len(byResult))
	for result, count := range byResult {
		items = append(items, coreschedule.SignFailureBucket{Result: result, Count: count})
	}
	sort.Slice(items, func(i, j int) bool {
		if items[i].Count != items[j].Count {
			return items[i].Count > items[j].Count
		}
		return items[i].Result < items[j].Result
	})
	return items, total
}
