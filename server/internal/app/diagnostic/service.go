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
// app 层用例。端口见本文件，实现见 adapters/pg.DiagnosticRepo。
//
// # 复用
// Service、Item、Detail、StatusReport；端口 Store、AccountSource；
// SuggestionFor 与 Suggestion 词表常量。
//
// # 口径
// 业务日按 Deps.Location 解析，与执行循环同口径。
// stale = now - sign_tasks.updated_at > StaleAfter。
// 一账号当日多任务行时取最近更新的一行：轮询用户关心的是
// 「现在怎么样」。payload/响应不含手机号、凭据与平台原文。
package diagnostic

import (
	"context"
	"errors"
	"time"

	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coremask "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// Suggestion 是给用户的封闭建议词表。
const (
	SuggestWait         = "wait"          // 系统处理中或当日已了结
	SuggestRetryNow     = "retry_now"     // 失败且窗口仍开：可手动再签
	SuggestReArm        = "re_arm"        // 失败且窗口已关或未知：当日无可为，等重排
	SuggestContactAdmin = "contact_admin" // 凭据不可读：只有管理员能修
)

// StaleAfter 是轮询的陈旧阈值：行 updated_at 距今超过它即 stale。
const StaleAfter = 90 * time.Second

// TaskRow 是当日任务行的只读形状（实现见 adapters/pg）。
type TaskRow struct {
	TaskID       string
	SchoolTaskID string
	AccountID    string
	State        string
	Result       string
	Attempts     int
	RunAt        time.Time
	UpdatedAt    time.Time
}

// WindowInfo 是当日窗口缓存行。
type WindowInfo struct {
	Start   time.Time
	End     time.Time
	Source  string
	Version int
}

// EgressInfo 是账号粘性出口的标签。
type EgressInfo struct {
	Identity string
}

// Store 是诊断只读仓储端口。账号集合先经作用域过滤，仓储按显式
// id 集查询——作用域安全由 AccountSource 保证，本端口不重复判。
type Store interface {
	TodayTasks(ctx context.Context, accountIDs []string, day string) ([]TaskRow, error)
	Windows(ctx context.Context, schoolTaskIDs []string, day string) (map[string]WindowInfo, error)
	Egresses(ctx context.Context, accountIDs []string) (map[string]EgressInfo, error)
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
	Clock    coreclock.Clock
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
	Window *WindowInfo
	Egress *EgressInfo
}

// StatusItem 是轮询条目。stale 的口径见 StaleAfter。
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
	day := coreclock.BusinessDay(now, s.deps.Location)
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
			item.Stale = now.Sub(row.UpdatedAt) > StaleAfter
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
	day := coreclock.BusinessDay(now, s.deps.Location)
	rows, err := s.deps.Store.TodayTasks(ctx, []string{record.ID}, day)
	if err != nil {
		return Detail{}, err
	}
	item := Item{AccountID: record.ID, Suggestion: SuggestWait}
	var row *TaskRow
	if len(rows) > 0 {
		r := latestByAccount(rows)[record.ID]
		row = &r
		state, runAt, result := r.State, r.RunAt, coremask.MaskText(r.Result)
		item.State, item.RunAt, item.Attempts, item.Result = &state, &runAt, r.Attempts, result
		if r.State == "pending" && r.RunAt.After(now) {
			next := r.RunAt
			item.NextRetryAt = &next
		}
	}
	var win *WindowInfo
	if row != nil {
		wins, err := s.deps.Store.Windows(ctx, []string{row.SchoolTaskID}, day)
		if err != nil {
			return Detail{}, err
		}
		if w, ok := wins[row.SchoolTaskID]; ok {
			win = &w
		}
	}
	item.Suggestion = SuggestionFor(item.stateOr(""), item.Result, win, now)
	egs, err := s.deps.Store.Egresses(ctx, []string{record.ID})
	if err != nil {
		return Detail{}, err
	}
	var eg *EgressInfo
	if e, ok := egs[record.ID]; ok {
		eg = &e
	}
	return Detail{Item: item, Window: win, Egress: eg}, nil
}

// View 是 SSE snapshot 帧的数据：业务日与状态计数。
type View struct {
	Day    string         `json:"day"`
	States map[string]int `json:"states"`
}

// Snapshot 返回作用域内当日的状态计数，供 SSE 的 snapshot 帧。
func (s *Service) Snapshot(ctx context.Context, scope corescope.Scope) (View, error) {
	now := s.deps.Clock.Now()
	counts, err := s.deps.Store.StateCounts(ctx, scope, coreclock.BusinessDay(now, s.deps.Location))
	if err != nil {
		return View{}, err
	}
	return View{Day: coreclock.BusinessDay(now, s.deps.Location), States: counts}, nil
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
	day := coreclock.BusinessDay(now, s.deps.Location)
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
		item := Item{AccountID: a.ID, Suggestion: SuggestWait}
		if row, ok := latest[a.ID]; ok {
			state, runAt, result := row.State, row.RunAt, coremask.MaskText(row.Result)
			item.State, item.RunAt, item.Attempts, item.Result = &state, &runAt, row.Attempts, result
			if row.State == "pending" && row.RunAt.After(now) {
				next := row.RunAt
				item.NextRetryAt = &next
			}
			var win *WindowInfo
			if w, ok := wins[row.SchoolTaskID]; ok {
				win = &w
			}
			item.Suggestion = SuggestionFor(row.State, row.Result, win, now)
		}
		items = append(items, item)
	}
	return Report{UpdatedAt: now, Items: items}, nil
}

// windowsForFailed 批量取失败行的当日窗口；无失败行时不查询。
func (s *Service) windowsForFailed(ctx context.Context, latest map[string]TaskRow, day string) (map[string]WindowInfo, error) {
	var taskIDs []string
	for _, r := range latest {
		if r.State == "failed" {
			taskIDs = append(taskIDs, r.SchoolTaskID)
		}
	}
	if len(taskIDs) == 0 {
		return nil, nil
	}
	return s.deps.Store.Windows(ctx, taskIDs, day)
}

// latestByAccount 每账号取一行：最近更新优先，同刻取更早的 RunAt。
func latestByAccount(rows []TaskRow) map[string]TaskRow {
	out := make(map[string]TaskRow, len(rows))
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

// stateOr 返回 Item.State 的字面值或空串。
func (i Item) stateOr(fallback string) string {
	if i.State == nil {
		return fallback
	}
	return *i.State
}

// SuggestionFor 按状态与结论词映射封闭建议词。
//
//   - failed + credential_invalid → contact_admin：只有管理员能修凭据；
//   - failed + 窗口仍开 → retry_now：手动再签是正当动作；
//   - failed + 窗口已关或未知 → re_arm：当日无可为，等系统重排；
//   - 其余状态（含未知状态）→ wait。
//
// 窗口未知按保守方向处理：不给 retry_now，避免用户白点一次。
// result 先经掩码：结论词本无敏感内容，此步是纵深防御。
func SuggestionFor(state, result string, win *WindowInfo, now time.Time) string {
	result = coremask.MaskText(result)
	if state != "failed" {
		return SuggestWait
	}
	if result == "credential_invalid" {
		return SuggestContactAdmin
	}
	if win != nil && now.Before(win.End) && !now.Before(win.Start) {
		return SuggestRetryNow
	}
	return SuggestReArm
}
