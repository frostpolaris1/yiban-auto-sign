// Package calendar 实现用户签到日历读模型（v1）。
//
// # 功能
// 按业务日范围汇总本人账号的签到结果，并给出单日逐账号结果。
// 计数按账号去重：同账号同日多行只取最近更新的一行；
// 重试、尝试与事件历史不参与计数。
// 本包**不**写库、**不**发 HTTP 请求、**不**判平台结果。
//
// # 归属
// app 层用例。端口见本文件，adapters/pg 实现。
// 范围、日期与时区判据只在用例层；存储层只按显式 userID 与日期查询。
//
// # 复用
// Service、Store、Deps；MaxRangeDays。
// 读模型类型在 core/schedule，适配器因此不必反向依赖 app。
//
// # 口径
// 只读当前用户 id 名下的账号；软删账号由存储层排除。
// 终态词表取 core/status；pending、running 与未知值归入 unknown。
// today 与 timezone 由服务端时钟与配置时区给出，客户端时钟不参与。
package calendar

import (
	"context"
	"errors"
	"sort"
	"time"

	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
)

// MaxRangeDays 是日历范围的最大天数（含首尾）。
const MaxRangeDays = 31

// Store 是日历只读仓储端口。
// 账号归属与软删判据必须在 SQL 内完成；本端口不接受作用域，
// 只接受会话 userID，防止调用方误传更宽的范围。
type Store interface {
	CalendarTasks(ctx context.Context, userID, from, to string) ([]coreschedule.CalendarTaskRow, error)
}

// Deps 是用例依赖。
type Deps struct {
	Store    Store
	Clock    coreclock.Clock
	Location *time.Location
}

// Service 是日历用例服务。
type Service struct {
	deps Deps
}

// NewService 构造服务。缺依赖即失败。
func NewService(deps Deps) (*Service, error) {
	if deps.Store == nil {
		return nil, errors.New("calendar: nil store")
	}
	if deps.Clock == nil {
		return nil, errors.New("calendar: nil clock")
	}
	if deps.Location == nil {
		return nil, errors.New("calendar: nil location")
	}
	return &Service{deps: deps}, nil
}

// Range 返回 from..to（含首尾）每天的账号级计数。
func (s *Service) Range(ctx context.Context, userID, from, to string) (coreschedule.CalendarRange, error) {
	if userID == "" {
		return coreschedule.CalendarRange{}, coreerrs.New(coreerrs.CodeInternal,
			"sign calendar requires a user")
	}
	fromDay, err := s.parseDay("from", from)
	if err != nil {
		return coreschedule.CalendarRange{}, err
	}
	toDay, err := s.parseDay("to", to)
	if err != nil {
		return coreschedule.CalendarRange{}, err
	}
	if toDay.Before(fromDay) {
		return coreschedule.CalendarRange{}, coreerrs.New(coreerrs.CodeValidation,
			"from must not be after to")
	}
	if days := dayNumber(toDay) - dayNumber(fromDay) + 1; days > MaxRangeDays {
		return coreschedule.CalendarRange{}, coreerrs.New(coreerrs.CodeValidation,
			"range must be at most 31 days")
	}

	rows, err := s.deps.Store.CalendarTasks(ctx, userID, from, to)
	if err != nil {
		return coreschedule.CalendarRange{}, err
	}
	latest, updatedAt := latestByAccountDay(rows)
	byDay := map[string]*coreschedule.CalendarDayCounts{}
	for key, row := range latest {
		counts, ok := byDay[key.Day]
		if !ok {
			counts = &coreschedule.CalendarDayCounts{Day: key.Day}
			byDay[key.Day] = counts
		}
		counts.Total++
		switch bucket(row.State) {
		case string(corestatus.TaskSuccess):
			counts.Success++
		case string(corestatus.TaskFailed):
			counts.Failed++
		case string(corestatus.TaskSkipped):
			counts.Skipped++
		default:
			counts.Unknown++
		}
	}

	now := s.deps.Clock.Now()
	today := coreclock.BusinessDay(now, s.deps.Location)
	if updatedAt.IsZero() {
		updatedAt = now
	}
	days := make([]coreschedule.CalendarDayCounts, 0, dayNumber(toDay)-dayNumber(fromDay)+1)
	for day := fromDay; !day.After(toDay); day = day.AddDate(0, 0, 1) {
		key := day.Format("2006-01-02")
		item := coreschedule.CalendarDayCounts{Day: key, Future: key > today}
		if !item.Future {
			if counts, ok := byDay[key]; ok {
				item.Total = counts.Total
				item.Success = counts.Success
				item.Failed = counts.Failed
				item.Skipped = counts.Skipped
				item.Unknown = counts.Unknown
			}
		}
		days = append(days, item)
	}
	return coreschedule.CalendarRange{
		Timezone:  s.deps.Location.String(),
		Today:     today,
		UpdatedAt: updatedAt,
		Days:      days,
	}, nil
}

// Day 返回一个业务日的逐账号结果。
// 没有任务的账号不出现；无数据的日期返回空列表而不是 404。
// 未来日与范围读面同口径：Future=true 且账号列表为空。
func (s *Service) Day(ctx context.Context, userID, day string) (coreschedule.CalendarDayDetail, error) {
	if userID == "" {
		return coreschedule.CalendarDayDetail{}, coreerrs.New(coreerrs.CodeInternal,
			"sign calendar requires a user")
	}
	if _, err := s.parseDay("day", day); err != nil {
		return coreschedule.CalendarDayDetail{}, err
	}
	today := coreclock.BusinessDay(s.deps.Clock.Now(), s.deps.Location)
	if day > today {
		return coreschedule.CalendarDayDetail{
			Day:      day,
			Timezone: s.deps.Location.String(),
			Accounts: []coreschedule.CalendarAccountOutcome{},
			Future:   true,
		}, nil
	}
	rows, err := s.deps.Store.CalendarTasks(ctx, userID, day, day)
	if err != nil {
		return coreschedule.CalendarDayDetail{}, err
	}
	latest, _ := latestByAccountDay(rows)
	accountIDs := make([]string, 0, len(latest))
	for key := range latest {
		accountIDs = append(accountIDs, key.AccountID)
	}
	sort.Strings(accountIDs)
	accounts := make([]coreschedule.CalendarAccountOutcome, 0, len(accountIDs))
	for _, accountID := range accountIDs {
		row := latest[dayKey{AccountID: accountID, Day: day}]
		var runAt *time.Time
		if !row.RunAt.IsZero() {
			at := row.RunAt
			runAt = &at
		}
		accounts = append(accounts, coreschedule.CalendarAccountOutcome{
			AccountID: accountID,
			State:     bucket(row.State),
			RunAt:     runAt,
			Attempts:  row.Attempts,
		})
	}
	return coreschedule.CalendarDayDetail{
		Day:      day,
		Timezone: s.deps.Location.String(),
		Accounts: accounts,
	}, nil
}

// dayKey 是账号去重的键：同一账号同一天只留一行。
type dayKey struct {
	AccountID string
	Day       string
}

// latestByAccountDay 按 (账号, 日期) 取最近更新的一行，并给出全范围最大更新时间。
// 存储层按更新时间倒序返回；这里再比较一次，排序变化不会让结果变旧。
// 同口径还出现在 adapters/pg/stats.go 的 DISTINCT ON 与
// adapters/pg/engine.go 的当日终态：
// tie-break 是 updated_at DESC, id DESC，同口径各处必须同改。
func latestByAccountDay(rows []coreschedule.CalendarTaskRow) (map[dayKey]coreschedule.CalendarTaskRow, time.Time) {
	latest := make(map[dayKey]coreschedule.CalendarTaskRow, len(rows))
	var updatedAt time.Time
	for _, row := range rows {
		if row.UpdatedAt.After(updatedAt) {
			updatedAt = row.UpdatedAt
		}
		key := dayKey{AccountID: row.AccountID, Day: row.Day}
		if cur, ok := latest[key]; !ok || row.UpdatedAt.After(cur.UpdatedAt) {
			latest[key] = row
		}
	}
	return latest, updatedAt
}

// bucket 把任务状态归一到日历四桶。
// 终态常量只从 core/status 取；其余一律 unknown。
func bucket(state string) string {
	switch corestatus.TaskState(state) {
	case corestatus.TaskSuccess:
		return string(corestatus.TaskSuccess)
	case corestatus.TaskFailed:
		return string(corestatus.TaskFailed)
	case corestatus.TaskSkipped:
		return string(corestatus.TaskSkipped)
	default:
		return coreschedule.CalendarUnknown
	}
}

// parseDay 只接受 YYYY-MM-DD 的真实日历日。
func (s *Service) parseDay(field, raw string) (time.Time, error) {
	day, err := time.ParseInLocation("2006-01-02", raw, s.deps.Location)
	if err != nil || day.Format("2006-01-02") != raw {
		return time.Time{}, coreerrs.New(coreerrs.CodeValidation,
			field+" must be YYYY-MM-DD")
	}
	return day, nil
}

// dayNumber 把本地日历日折算成无时区的日序号。
// 用 UTC 重建年月日：夏令时不会让两个相邻日期相差 23 或 25 小时。
func dayNumber(day time.Time) int {
	return int(time.Date(day.Year(), day.Month(), day.Day(), 0, 0, 0, 0, time.UTC).Unix() / 86400)
}
