// Package diagnostic 实现账号诊断与轮询读模型（P6）。
//
// # 功能
// /api/me/diagnostics、/api/accounts/{id}/diagnostics、
// /api/me/sign-status 三端点与 SSE snapshot 的数据用例。
// 全部只读：任务行、窗口缓存与出口标签来自 Store 端口，
// 账号可见面来自 AccountSource（复用 app/account 的作用域判据）。
// 本包**不**写库、**不**实现 HTTP、**不**碰凭据。
//
// # 归属
// app 层用例。端口见本文件，实现见 adapters/pg.DiagnosticRepo；
// 领域形状与建议词表在 core/diagnostic，端口与其共享类型，
// 适配器因此不必反向依赖 app 层。
//
// # 复用
// Service、Item、Report、Detail、StatusReport；端口 Store、AccountSource。
//
// # 口径
// 业务日按 Deps.Location 解析，与执行循环同口径。
// stale = now - sign_tasks.updated_at > corediag.StaleAfter。
// 一账号当日多任务行时取最近更新的一行：轮询用户关心的是
// 「现在怎么样」。响应不含手机号、凭据与平台原文。
package diagnostic

import (
	"context"
	"errors"
	"time"

	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	corediag "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/diagnostic"
	coremask "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
)

// Store 是诊断只读仓储端口。账号集合先经作用域过滤，仓储按显式
// id 集查询——作用域安全由 AccountSource 保证，本端口不重复判。
type Store interface {
	TodayTasks(ctx context.Context, accountIDs []string, day string) ([]corediag.TaskRow, error)
	Windows(ctx context.Context, schoolTaskIDs []string, day string) (map[string]corediag.WindowInfo, error)
	Egresses(ctx context.Context, accountIDs []string) (map[string]corediag.EgressInfo, error)
	StateCounts(ctx context.Context, scope corescope.Scope, day string) (map[string]int, error)
}

// AccountSource 复用账号用例的可见面判据（实现为 *appaccount.Service）。
// 跨用户与跨校读取一律 not_found。
type AccountSource interface {
	List(ctx context.Context, scope corescope.Scope, filter appaccount.ListFilter, cursor string, limit int) (appaccount.Page, error)
	Get(ctx context.Context, scope corescope.Scope, id string) (appaccount.Record, error)
}

// Deps 是诊断用例的依赖。
type Deps struct {
	Accounts AccountSource
	Store    Store
	Clock    clock.Clock
	Location *time.Location
}

// Service 是诊断与轮询用例服务。
type Service struct {
	deps Deps
}

// NewService 构造服务。缺依赖即失败。
func NewService(deps Deps) (*Service, error) {
	if deps.Accounts == nil {
		return nil, errors.New("diagnostic: nil account source")
	}
	if deps.Store == nil {
		return nil, errors.New("diagnostic: nil store")
	}
	if deps.Clock == nil {
		return nil, errors.New("diagnostic: nil clock")
	}
	return &Service{deps: deps}, nil
}

// Item 是一条账号诊断。
// State 为 nil 表示该账号当日没有任务行。
type Item struct {
	AccountID   string
	State       *string
	RunAt       *time.Time
	Attempts    int
	Result      string
	NextRetryAt *time.Time
	Suggestion  string
}

// Report 是 /api/me/diagnostics 的响应体。
type Report struct {
	UpdatedAt time.Time
	Items     []Item
}

// Detail 是单账号详版：诊断项加窗口与出口标签。
type Detail struct {
	Item
	Window *corediag.WindowInfo
	Egress *corediag.EgressInfo
}

// StatusItem 是轮询条目。stale 的口径见 corediag.StaleAfter。
type StatusItem struct {
	AccountID string
	State     *string
	RunAt     *time.Time
	Stale     bool
}

// StatusReport 是 /api/me/sign-status 的响应体。
type StatusReport struct {
	UpdatedAt time.Time
	Items     []StatusItem
}

// pageLimit 是列账号的分页大小：账号上限一万，200 一页至多 50 页。
const pageLimit = 200

// Mine 列当前用户账号的当日诊断。作用域恒为 User(uid)。
func (s *Service) Mine(ctx context.Context, userID string) (Report, error) {
	accounts, err := s.ownAccounts(ctx, corescope.User(userID))
	if err != nil {
		return Report{}, err
	}
	return s.report(ctx, accounts)
}

// SignStatus 列当前用户账号的轮询状态。作用域恒为 User(uid)。
func (s *Service) SignStatus(ctx context.Context, userID string) (StatusReport, error) {
	accounts, err := s.ownAccounts(ctx, corescope.User(userID))
	if err != nil {
		return StatusReport{}, err
	}
	now := s.deps.Clock.Now()
	day := clock.BusinessDay(now, s.deps.Location)
	rows, err := s.deps.Store.TodayTasks(ctx, accountIDs(accounts), day)
	if err != nil {
		return StatusReport{}, err
	}
	latest := latestByAccount(rows)
	items := make([]StatusItem, 0, len(accounts))
	for _, a := range accounts {
		item := StatusItem{AccountID: a.ID}
		if row, ok := latest[a.ID]; ok {
			state, runAt := row.State, row.RunAt
			item.State, item.RunAt = &state, &runAt
			item.Stale = now.Sub(row.UpdatedAt) > corediag.StaleAfter
		}
		items = append(items, item)
	}
	return StatusReport{UpdatedAt: now, Items: items}, nil
}

// Account 读单账号详版。跨用户与跨校由 AccountSource 判 404。
func (s *Service) Account(ctx context.Context, scope corescope.Scope, accountID string) (Detail, error) {
	record, err := s.deps.Accounts.Get(ctx, scope, accountID)
	if err != nil {
		return Detail{}, err
	}
	now := s.deps.Clock.Now()
	day := clock.BusinessDay(now, s.deps.Location)
	rows, err := s.deps.Store.TodayTasks(ctx, []string{record.ID}, day)
	if err != nil {
		return Detail{}, err
	}
	item := Item{AccountID: record.ID, Suggestion: corediag.SuggestWait}
	var row *corediag.TaskRow
	if len(rows) > 0 {
		r := latestByAccount(rows)[record.ID]
		row = &r
		state, runAt, result := row.State, row.RunAt, coremask.MaskText(row.Result)
		item.State, item.RunAt, item.Attempts, item.Result = &state, &runAt, row.Attempts, result
		if corestatus.TaskState(row.State) == corestatus.TaskPending && row.RunAt.After(now) {
			next := row.RunAt
			item.NextRetryAt = &next
		}
	}
	var win *corediag.WindowInfo
	if row != nil {
		wins, err := s.deps.Store.Windows(ctx, []string{row.SchoolTaskID}, day)
		if err != nil {
			return Detail{}, err
		}
		if w, ok := wins[row.SchoolTaskID]; ok {
			win = &w
		}
	}
	state := ""
	if item.State != nil {
		state = *item.State
	}
	item.Suggestion = corediag.SuggestionFor(state, item.Result, win, now)
	egs, err := s.deps.Store.Egresses(ctx, []string{record.ID})
	if err != nil {
		return Detail{}, err
	}
	var eg *corediag.EgressInfo
	if e, ok := egs[record.ID]; ok {
		eg = &e
	}
	return Detail{Item: item, Window: win, Egress: eg}, nil
}

// Snapshot 返回作用域内当日的状态计数，供 SSE 的 snapshot 帧。
func (s *Service) Snapshot(ctx context.Context, scope corescope.Scope) (corediag.View, error) {
	now := s.deps.Clock.Now()
	day := clock.BusinessDay(now, s.deps.Location)
	counts, err := s.deps.Store.StateCounts(ctx, scope, day)
	if err != nil {
		return corediag.View{}, err
	}
	return corediag.View{Day: day, States: counts}, nil
}

// ownAccounts 分页拉取作用域内全部账号。
func (s *Service) ownAccounts(ctx context.Context, scope corescope.Scope) ([]appaccount.Record, error) {
	var out []appaccount.Record
	cursor := ""
	for {
		page, err := s.deps.Accounts.List(ctx, scope, appaccount.ListFilter{}, cursor, pageLimit)
		if err != nil {
			return nil, err
		}
		out = append(out, page.Items...)
		if page.NextCursor == "" {
			return out, nil
		}
		cursor = page.NextCursor
	}
}

// report 组装 Mine 的响应体。
func (s *Service) report(ctx context.Context, accounts []appaccount.Record) (Report, error) {
	now := s.deps.Clock.Now()
	day := clock.BusinessDay(now, s.deps.Location)
	rows, err := s.deps.Store.TodayTasks(ctx, accountIDs(accounts), day)
	if err != nil {
		return Report{}, err
	}
	latest := latestByAccount(rows)
	// 失败行需要窗口信息才能区分 retry_now 与 re_arm；一次批量取。
	wins, err := s.windowsForFailed(ctx, latest, day)
	if err != nil {
		return Report{}, err
	}
	items := make([]Item, 0, len(accounts))
	for _, a := range accounts {
		item := Item{AccountID: a.ID, Suggestion: corediag.SuggestWait}
		if row, ok := latest[a.ID]; ok {
			state, runAt, result := row.State, row.RunAt, coremask.MaskText(row.Result)
			item.State, item.RunAt, item.Attempts, item.Result = &state, &runAt, row.Attempts, result
			if corestatus.TaskState(row.State) == corestatus.TaskPending && row.RunAt.After(now) {
				next := row.RunAt
				item.NextRetryAt = &next
			}
			var win *corediag.WindowInfo
			if w, ok := wins[row.SchoolTaskID]; ok {
				win = &w
			}
			item.Suggestion = corediag.SuggestionFor(row.State, row.Result, win, now)
		}
		items = append(items, item)
	}
	return Report{UpdatedAt: now, Items: items}, nil
}

// windowsForFailed 批量取失败行的当日窗口；无失败行时不查询。
func (s *Service) windowsForFailed(ctx context.Context, latest map[string]corediag.TaskRow, day string) (map[string]corediag.WindowInfo, error) {
	var taskIDs []string
	for _, r := range latest {
		if corestatus.TaskState(r.State) == corestatus.TaskFailed {
			taskIDs = append(taskIDs, r.SchoolTaskID)
		}
	}
	if len(taskIDs) == 0 {
		return nil, nil
	}
	return s.deps.Store.Windows(ctx, taskIDs, day)
}

// latestByAccount 每账号取一行：最近更新优先，同刻取更早的 RunAt。
func latestByAccount(rows []corediag.TaskRow) map[string]corediag.TaskRow {
	out := make(map[string]corediag.TaskRow, len(rows))
	for _, r := range rows {
		cur, ok := out[r.AccountID]
		if !ok || r.UpdatedAt.After(cur.UpdatedAt) ||
			(r.UpdatedAt.Equal(cur.UpdatedAt) && r.RunAt.Before(cur.RunAt)) {
			out[r.AccountID] = r
		}
	}
	return out
}

func accountIDs(accounts []appaccount.Record) []string {
	out := make([]string, len(accounts))
	for i, a := range accounts {
		out[i] = a.ID
	}
	return out
}
