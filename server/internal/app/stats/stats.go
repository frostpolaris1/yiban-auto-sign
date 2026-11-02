// Package stats 实现看板聚合用例。
//
// # 功能
// 按作用域读签到终态与自选时间片人数；补齐缺失日期。
// 本包**不**写 SQL、**不**发 HTTP 请求、**不**算窗口。
//
// # 归属
// app 层用例。端口见本文件，adapters/pg 实现；
// 窗口与容量仍由 app/schedule 提供。
//
// # 复用
// Service、Store、Report、SignStats。
//
// # 口径
// 日桶互斥：同一账号同日的多任务行按最后更新收敛为一条，日内相加合法。
// ByStatus 是窗口内按状态去重的账号数，桶间可有交集，不得相加当 Total。
// 缺失日期补零并列入 DaysMissing：真 0 与没有数据必须可分。
// 只给 master 与 admin；user 的今日状态走 /api/me/sign-status。

package stats

import (
	"context"
	"errors"
	"strings"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// maxWindowDays 是一次看板聚合的最长窗口。
const maxWindowDays = 90

// Store 是看板聚合端口。
//
// SignStats 按作用域读 [from, to] 的日/状态计数；
// 只返回库里出现过的日期，缺失日期由用例层补齐。
// OffsetCounts 按偏移返回在该校有活跃账号的用户数；
// 同一用户在该校有多个账号只计一次。
type Store interface {
	SignStats(ctx context.Context, scope corescope.Scope, from, to string) (coreschedule.SignStats, error)
	OffsetCounts(ctx context.Context, schoolID string) (map[int]int, error)
}

// Service 是看板聚合服务。
type Service struct {
	store Store
}

// NewService 构造服务。缺依赖即失败。
func NewService(store Store) (*Service, error) {
	if store == nil {
		return nil, errors.New("stats: nil store")
	}
	return &Service{store: store}, nil
}

// Report 是补齐日期后的签到统计。
type Report struct {
	Days        []coreschedule.SignStatDay
	Total       int
	ByStatus    coreschedule.SignStatCounts
	DaysMissing []string
}

// SignStats 读 [from, to] 的账号终态统计。日期为 YYYY-MM-DD。
// 窗口最长 90 天；from 晚于 to 或越界回 400。
func (s *Service) SignStats(ctx context.Context, scope corescope.Scope, from, to string) (Report, error) {
	if err := app.RequireScope(scope); err != nil {
		return Report{}, err
	}
	if scope.Kind() != corescope.KindMaster && scope.Kind() != corescope.KindAdmin {
		return Report{}, coreerrs.New(coreerrs.CodeForbidden, "role cannot read sign statistics")
	}
	fromDate, err := time.Parse("2006-01-02", from)
	if err != nil {
		return Report{}, coreerrs.New(coreerrs.CodeValidation, "from must be YYYY-MM-DD")
	}
	toDate, err := time.Parse("2006-01-02", to)
	if err != nil {
		return Report{}, coreerrs.New(coreerrs.CodeValidation, "to must be YYYY-MM-DD")
	}
	if toDate.Before(fromDate) {
		return Report{}, coreerrs.New(coreerrs.CodeValidation, "to must not be before from")
	}
	if days := int(toDate.Sub(fromDate).Hours()/24) + 1; days > maxWindowDays {
		return Report{}, coreerrs.New(coreerrs.CodeValidation, "window must be at most 90 days")
	}

	data, err := s.store.SignStats(ctx, scope, from, to)
	if err != nil {
		return Report{}, err
	}
	byDay := make(map[string]coreschedule.SignStatDay, len(data.Days))
	for _, day := range data.Days {
		byDay[day.Day] = day
	}
	report := Report{
		Total:       data.Total,
		ByStatus:    data.ByStatus,
		Days:        make([]coreschedule.SignStatDay, 0, len(data.Days)),
		DaysMissing: make([]string, 0),
	}
	for day := fromDate; !day.After(toDate); day = day.AddDate(0, 0, 1) {
		key := day.Format("2006-01-02")
		if row, ok := byDay[key]; ok {
			report.Days = append(report.Days, row)
			continue
		}
		report.Days = append(report.Days, coreschedule.SignStatDay{Day: key})
		report.DaysMissing = append(report.DaysMissing, key)
	}
	return report, nil
}

// OffsetCounts 读某校自选偏移的人数分布。
// schoolID 为空或全是空白回 400。
func (s *Service) OffsetCounts(ctx context.Context, schoolID string) (map[int]int, error) {
	if strings.TrimSpace(schoolID) == "" {
		return nil, coreerrs.New(coreerrs.CodeValidation, "school_id is required")
	}
	return s.store.OffsetCounts(ctx, schoolID)
}
