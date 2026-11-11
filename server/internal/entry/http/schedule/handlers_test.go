package schedule_test

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schedule"
	appstats "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/stats"
	apptimepref "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/timepref"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/router"
	schedulehttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/schedule"
)

// ---- 测试替身 ----

type fakePlanner struct {
	preview    appschedule.Preview
	previewErr error
	buildErr   error
	task       coreschedule.TaskInfo
	taskErr    error
	views      []coreschedule.WindowView
	listErr    error

	builds      int
	reportCalls int
	// overrides 数覆盖用例调用次数：周末门关闭时不得写覆盖。
	overrides int
	// previewCalls / previewCachedCalls 区分 GET 只读预览与 POST 强制刷新。
	previewCalls       int
	previewCachedCalls int
	lastInput          appschedule.Input
	lastActor          string
	lastStart          time.Time
	lastEnd            time.Time
	lastReason         string
	overrideErr        error
	lastRefresh        bool

	self       coreschedule.SelfContext
	selfErr    error
	selfUserID string
	selfDay    string

	cached       coreschedule.WindowRecord
	cachedErr    error
	cachedCalls  int
	lastCachedIn appschedule.Input
}

// SelfContext 记录用户读面的解析输入；未设置时返回零值（无账号）。
func (f *fakePlanner) SelfContext(_ context.Context, userID, day string) (coreschedule.SelfContext, error) {
	f.selfUserID, f.selfDay = userID, day
	return f.self, f.selfErr
}

// CachedWindow 记录用户读面的窗口输入；不触发 Report/Discover。
func (f *fakePlanner) CachedWindow(_ context.Context, in appschedule.Input) (coreschedule.WindowRecord, error) {
	f.cachedCalls++
	f.lastCachedIn = in
	return f.cached, f.cachedErr
}

func (f *fakePlanner) Preview(_ context.Context, in appschedule.Input) (appschedule.Preview, error) {
	f.previewCalls++
	f.lastInput = in
	if in.WeekendClosed {
		return fakeClosedPreview(in), nil
	}
	return f.preview, f.previewErr
}

// PreviewCached 是 GET 路径：只读缓存，不发现平台（F5）。
func (f *fakePlanner) PreviewCached(_ context.Context, in appschedule.Input) (appschedule.Preview, error) {
	f.previewCachedCalls++
	f.lastInput = in
	if in.WeekendClosed {
		return fakeClosedPreview(in), nil
	}
	return f.preview, f.previewErr
}

// Report 记录调用次数与输入：容量报告必须走 Report 而不是 Preview（k0y-4）。
func (f *fakePlanner) Report(_ context.Context, in appschedule.Input) (appschedule.Preview, error) {
	f.reportCalls++
	f.lastInput = in
	return f.preview, f.previewErr
}

func (f *fakePlanner) Build(_ context.Context, in appschedule.Input) (appschedule.Preview, error) {
	f.builds++
	f.lastInput = in
	if in.WeekendClosed {
		return fakeClosedPreview(in), nil
	}
	return f.preview, f.buildErr
}

func (f *fakePlanner) Override(_ context.Context, _ corescope.Scope, in appschedule.Input,
	start, end time.Time, reason, actorID string) (appschedule.Preview, error) {
	f.overrides++
	f.lastInput, f.lastStart, f.lastEnd = in, start, end
	f.lastReason, f.lastActor = reason, actorID
	if in.WeekendClosed {
		return fakeClosedPreview(in), nil
	}
	return f.preview, f.overrideErr
}

// fakeClosedPreview 模拟真实用例层的周末门回执：0 行、来源 weekend_closed。
func fakeClosedPreview(in appschedule.Input) appschedule.Preview {
	return appschedule.Preview{
		Day: in.Day, Source: "weekend_closed", Note: appschedule.WeekendGateNote,
	}
}

func (f *fakePlanner) Task(context.Context, corescope.Scope, string) (coreschedule.TaskInfo, error) {
	return f.task, f.taskErr
}

func (f *fakePlanner) ListWindows(_ context.Context, _ corescope.Scope, _ string, refresh bool) ([]coreschedule.WindowView, error) {
	f.lastRefresh = refresh
	return f.views, f.listErr
}

type fakeTimePrefs struct {
	view        apptimepref.View
	err         error
	lastOffset  *int
	lastGetUser string
	lastSetUser string
	sets        int
}

func (f *fakeTimePrefs) Get(_ context.Context, userID string) (apptimepref.View, error) {
	f.lastGetUser = userID
	return f.view, f.err
}

func (f *fakeTimePrefs) Set(_ context.Context, userID string, offset *int) (apptimepref.View, error) {
	f.sets++
	f.lastSetUser = userID
	f.lastOffset = offset
	return f.view, f.err
}

// fakeStats 只供自选读面的拥挤度：SignStats 不在本文件断言面。
type fakeStats struct {
	counts map[int]int
	err    error
	// taskCalls 记录任务级端口被调用次数；旧全校端口保留为兼容回退。
	taskCalls int
	lastTask  string
	lastDay   string
}

func (f *fakeStats) SignStats(context.Context, corescope.Scope, string, string) (appstats.Report, error) {
	return appstats.Report{}, nil
}

func (f *fakeStats) OffsetCountsForTask(_ context.Context, _, taskID, day string) (map[int]int, error) {
	f.taskCalls++
	f.lastTask, f.lastDay = taskID, day
	return f.counts, f.err
}

func (f *fakeStats) OffsetCounts(context.Context, string) (map[int]int, error) {
	return f.counts, f.err
}

type fakeAuthorizer struct {
	scope corescope.Scope
	err   error
}

func (f *fakeAuthorizer) Authorize(context.Context, coreauth.User) (corescope.Scope, appaudit.Actor, error) {
	return f.scope, appaudit.Actor{}, f.err
}

type fakeIssuer struct {
	secret []byte
	token  string
	sess   coreauth.Session
}

func (f *fakeIssuer) Issue(_ context.Context, user coreauth.User) (coreauth.Issued, error) {
	const token = "test-session-token"
	session := coreauth.Session{User: user, CSRFToken: coreauth.CSRFToken(f.secret, token)}
	f.token, f.sess = token, session
	return coreauth.Issued{Token: token, Session: session}, nil
}

func (f *fakeIssuer) Resolve(_ context.Context, token string) (coreauth.Session, error) {
	if token != f.token {
		return coreauth.Session{}, errors.New("unknown session")
	}
	return f.sess, nil
}

func (f *fakeIssuer) Revoke(context.Context, string) error           { return nil }
func (f *fakeIssuer) RevokeAllForUser(context.Context, string) error { return nil }

var shanghai = time.FixedZone("CST", 8*3600)

// 计划预览的固定形状：一行、一个直方图桶、一个窗口。
func samplePreview() appschedule.Preview {
	start := time.Date(2026, 10, 29, 6, 30, 0, 0, shanghai)
	return appschedule.Preview{
		Day: "2026-10-29", Source: "platform",
		WindowStart: start, WindowEnd: start.Add(80 * time.Minute),
		Version: 3, Capacity: 64, Demand: 1, Shortfall: 0,
		Histogram: map[int]int{0: 1},
		Rows:      []appschedule.Row{{AccountID: "acct-1", RunAt: start}},
	}
}

func newRouter(t *testing.T, scope corescope.Scope, planner schedulehttp.Planner, prefs schedulehttp.TimePrefs) (http.Handler, coreauth.Issued) {
	t.Helper()
	return newRouterFull(t, scope, planner, prefs, &fakeStats{}, nil)
}

func newRouterWithStats(t *testing.T, scope corescope.Scope, planner schedulehttp.Planner,
	prefs schedulehttp.TimePrefs, stats schedulehttp.Stats) (http.Handler, coreauth.Issued) {
	t.Helper()
	if stats == nil {
		stats = &fakeStats{}
	}
	return newRouterFull(t, scope, planner, prefs, stats, nil)
}

func newRouterFull(t *testing.T, scope corescope.Scope, planner schedulehttp.Planner,
	prefs schedulehttp.TimePrefs, stats schedulehttp.Stats, logger *slog.Logger) (http.Handler, coreauth.Issued) {
	t.Helper()
	return newRouterAt(t, scope, planner, prefs, stats, logger,
		time.Date(2026, 10, 29, 8, 0, 0, 0, shanghai), nil)
}

// newRouterAt 用指定时钟与覆盖链构造路由：周末门与 sign_dist 的每请求
// 解析需要可控的业务日与可注入的 Overrides。
func newRouterAt(t *testing.T, scope corescope.Scope, planner schedulehttp.Planner,
	prefs schedulehttp.TimePrefs, stats schedulehttp.Stats, logger *slog.Logger,
	now time.Time, ov schedulehttp.ScheduleOverrides) (http.Handler, coreauth.Issued) {
	t.Helper()
	issuer := &fakeIssuer{secret: []byte("0123456789abcdef0123456789abcdef")}
	issued, err := issuer.Issue(context.Background(), coreauth.User{
		ID: "u1", Email: "boss@example.com", Role: coreauth.RoleMaster, Status: coreauth.StatusActive,
	})
	if err != nil {
		t.Fatalf("issue session: %v", err)
	}
	handler := schedulehttp.NewHandler(planner, prefs, &fakeAuthorizer{scope: scope}, schedulehttp.Config{
		Location: shanghai, Clock: coreclock.Fixed{T: now}, Workers: 6, Avg: 3 * time.Second, Rate: 1.0, Util: 0.8,
		SlotsPerSlice: 10, SignDist: appschedule.SignDistFront, UsePrefs: true,
		EdgeFront: 30 * time.Second, EdgeBack: 30 * time.Second,
		Overrides: ov,
		Logger:    logger,
	})
	if stats != nil {
		handler = handler.WithStats(stats)
	}
	engine, err := router.New(router.Options{Sessions: issuer, InsecureCookies: true, Schedule: handler})
	if err != nil {
		t.Fatalf("router: %v", err)
	}
	return engine, issued
}

func do(t *testing.T, engine http.Handler, issued coreauth.Issued, method, path string, body any) *httptest.ResponseRecorder {
	t.Helper()
	var reader *bytes.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			t.Fatalf("marshal: %v", err)
		}
		reader = bytes.NewReader(raw)
	} else {
		reader = bytes.NewReader(nil)
	}
	req := httptest.NewRequest(method, path, reader)
	req.AddCookie(&http.Cookie{Name: coreauth.InsecureCookieName, Value: issued.Token})
	req.Header.Set("X-CSRF-Token", issued.CSRFToken)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	rec := httptest.NewRecorder()
	engine.ServeHTTP(rec, req)
	return rec
}

// ---- 用例 ----

// 窗口列表：缓存与未缓存两种行都如实下发。
func TestWindowsList(t *testing.T) {
	start := time.Date(2026, 10, 29, 6, 30, 0, 0, shanghai)
	end := start.Add(80 * time.Minute)
	planner := &fakePlanner{views: []coreschedule.WindowView{
		{SchoolID: "s1", SchoolName: "一中", TaskID: "t1", TaskName: "晨签",
			Enabled: true, Day: "2026-10-29", Start: start, End: end,
			Source: "platform", Version: 2, Cached: true},
		{SchoolID: "s2", SchoolName: "二中", TaskID: "t2", TaskName: "午签",
			Enabled: true, Day: "2026-10-29"},
	}}
	engine, issued := newRouter(t, corescope.Master(), planner, &fakeTimePrefs{})
	rec := do(t, engine, issued, http.MethodGet, "/api/schedule/windows?day=2026-10-29", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d: %s", rec.Code, rec.Body.String())
	}
	var body oapi.ScheduleWindowList
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.Day != "2026-10-29" || len(body.Items) != 2 {
		t.Fatalf("body = %+v", body)
	}
	if body.Items[0].Start == nil || body.Items[0].Source == nil || *body.Items[0].Source != "platform" {
		t.Fatalf("缓存行丢字段: %+v", body.Items[0])
	}
	if body.Items[1].Start != nil || body.Items[1].Source != nil {
		t.Fatalf("未缓存行不应带窗口: %+v", body.Items[1])
	}
	// k0y-4：缺省不刷新。
	if planner.lastRefresh {
		t.Fatal("缺省 refresh 必须为假：默认只读缓存")
	}
	// F5：GET 的 refresh 参数被忽略，不再触发平台发现。
	rec = do(t, engine, issued, http.MethodGet, "/api/schedule/windows?day=2026-10-29&refresh=true", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("refresh status = %d: %s", rec.Code, rec.Body.String())
	}
	if planner.lastRefresh {
		t.Fatal("GET refresh 必须被忽略：GET 路径不得触发平台发现")
	}
	// 强制刷新走 POST 变体。
	rec = do(t, engine, issued, http.MethodPost, "/api/schedule/windows/refresh?day=2026-10-29", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("POST refresh status = %d: %s", rec.Code, rec.Body.String())
	}
	if !planner.lastRefresh {
		t.Fatal("POST refresh 必须透传 refresh=true")
	}
}

// 非 master/admin 不得看计划管理面。
func TestWindowsUserForbidden(t *testing.T) {
	engine, issued := newRouter(t, corescope.User("u1"), &fakePlanner{}, &fakeTimePrefs{})
	rec := do(t, engine, issued, http.MethodGet, "/api/schedule/windows?day=2026-10-29", nil)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403: %s", rec.Code, rec.Body.String())
	}
}

// 预览：任务与学校必须配套；响应含行与直方图。
func TestPreview(t *testing.T) {
	planner := &fakePlanner{
		preview: samplePreview(),
		task:    coreschedule.TaskInfo{ID: "t1", SchoolID: "s1", Name: "晨签", Enabled: true},
	}
	engine, issued := newRouter(t, corescope.Master(), planner, &fakeTimePrefs{})
	rec := do(t, engine, issued, http.MethodGet,
		"/api/schedule/preview?day=2026-10-29&school_id=s1&task_id=t1", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d: %s", rec.Code, rec.Body.String())
	}
	var body oapi.SchedulePlan
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.Source != "platform" || body.Capacity != 64 || len(body.Rows) != 1 {
		t.Fatalf("body = %+v", body)
	}
	if body.Rows[0].AccountId != "acct-1" || body.Histogram["0"] != 1 {
		t.Fatalf("行或直方图丢失: %+v", body)
	}
	if planner.lastInput.EdgeFront != 30*time.Second || !planner.lastInput.UsePrefs {
		t.Fatalf("装配参数未传入: %+v", planner.lastInput)
	}
	// F5：GET 走只读预览，不触发平台发现。
	if planner.previewCachedCalls != 1 || planner.previewCalls != 0 {
		t.Fatalf("GET preview calls = cached %d / live %d, want 1/0",
			planner.previewCachedCalls, planner.previewCalls)
	}
	// 强制刷新走 POST 变体：实时发现但仍不写库。
	rec = do(t, engine, issued, http.MethodPost,
		"/api/schedule/preview/refresh?day=2026-10-29&school_id=s1&task_id=t1", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("POST refresh status = %d: %s", rec.Code, rec.Body.String())
	}
	if planner.previewCalls != 1 {
		t.Fatalf("POST refresh 未走实时预览：live calls = %d", planner.previewCalls)
	}
}

// 任务属于别的学校：参数错误 400；任务不存在：404。
func TestPreviewTaskMismatch(t *testing.T) {
	planner := &fakePlanner{
		preview: samplePreview(),
		task:    coreschedule.TaskInfo{ID: "t1", SchoolID: "s1"},
	}
	engine, issued := newRouter(t, corescope.Master(), planner, &fakeTimePrefs{})
	rec := do(t, engine, issued, http.MethodGet,
		"/api/schedule/preview?day=2026-10-29&school_id=s2&task_id=t1", nil)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400: %s", rec.Code, rec.Body.String())
	}

	planner.taskErr = errs.New(errs.CodeNotFound, "school task not found")
	engine, issued = newRouter(t, corescope.Master(), planner, &fakeTimePrefs{})
	rec = do(t, engine, issued, http.MethodGet,
		"/api/schedule/preview?day=2026-10-29&school_id=s1&task_id=t1", nil)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404: %s", rec.Code, rec.Body.String())
	}
}

// 建计划只接受当日：非当前业务日回 400，且不调用用例。
func TestPlanRejectsNonCurrentDay(t *testing.T) {
	planner := &fakePlanner{
		preview: samplePreview(),
		task:    coreschedule.TaskInfo{ID: "t1", SchoolID: "s1"},
	}
	engine, issued := newRouter(t, corescope.Master(), planner, &fakeTimePrefs{})
	rec := do(t, engine, issued, http.MethodPost, "/api/schedule/plan",
		oapi.SchedulePlanRequest{Day: "2026-10-28", SchoolId: "s1", TaskId: "t1"})
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400: %s", rec.Code, rec.Body.String())
	}
	if planner.builds != 0 {
		t.Fatalf("builds = %d, want 0（非当日不得调用用例）", planner.builds)
	}
}

// 建计划：请求体字段全部落到输入，返回落库版本。
func TestPlanBuilds(t *testing.T) {
	planner := &fakePlanner{
		preview: samplePreview(),
		task:    coreschedule.TaskInfo{ID: "t1", SchoolID: "s1"},
	}
	engine, issued := newRouter(t, corescope.Master(), planner, &fakeTimePrefs{})
	rec := do(t, engine, issued, http.MethodPost, "/api/schedule/plan",
		oapi.SchedulePlanRequest{Day: "2026-10-29", SchoolId: "s1", TaskId: "t1"})
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d: %s", rec.Code, rec.Body.String())
	}
	if planner.builds != 1 {
		t.Fatalf("builds = %d, want 1", planner.builds)
	}
	if planner.lastInput.Day != "2026-10-29" || planner.lastInput.SchoolID != "s1" ||
		planner.lastInput.SchoolTaskID != "t1" {
		t.Fatalf("输入 = %+v", planner.lastInput)
	}
	var body oapi.SchedulePlan
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.Version != 3 {
		t.Fatalf("版本 = %d, want 3", body.Version)
	}
}

// 容量缺口：409 capacity_shortfall，details 带三个数（C2）。
func TestPlanShortfallIsConflict(t *testing.T) {
	shortfall := errs.New(errs.CodeCapacityShortfall, "容量不足：需求 100，容量 64")
	shortfall.Details = map[string]any{"demand": 100, "capacity": 64, "shortfall": 36}
	planner := &fakePlanner{
		buildErr: shortfall,
		task:     coreschedule.TaskInfo{ID: "t1", SchoolID: "s1"},
	}
	engine, issued := newRouter(t, corescope.Master(), planner, &fakeTimePrefs{})
	rec := do(t, engine, issued, http.MethodPost, "/api/schedule/plan",
		oapi.SchedulePlanRequest{Day: "2026-10-29", SchoolId: "s1", TaskId: "t1"})
	if rec.Code != http.StatusConflict {
		t.Fatalf("status = %d, want 409: %s", rec.Code, rec.Body.String())
	}
	var body struct {
		Error struct {
			Code    string         `json:"code"`
			Details map[string]any `json:"details"`
		} `json:"error"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.Error.Code != "capacity_shortfall" {
		t.Fatalf("code = %q, want capacity_shortfall", body.Error.Code)
	}
	if body.Error.Details["demand"] != float64(100) {
		t.Fatalf("details = %v", body.Error.Details)
	}
}

// 覆盖：理由、起止与操作者原样交给用例。
func TestOverridePassesReasonAndActor(t *testing.T) {
	planner := &fakePlanner{preview: samplePreview()}
	engine, issued := newRouter(t, corescope.Master(), planner, &fakeTimePrefs{})
	start := "2026-10-29T09:00:00+08:00"
	end := "2026-10-29T09:30:00+08:00"
	body := map[string]string{
		"day": "2026-10-29", "school_id": "s1", "task_id": "t1",
		"start": start, "end": end, "reason": "平台窗口错误",
	}
	rec := do(t, engine, issued, http.MethodPost, "/api/schedule/override", body)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d: %s", rec.Code, rec.Body.String())
	}
	if planner.lastReason != "平台窗口错误" || planner.lastActor != "u1" {
		t.Fatalf("reason=%q actor=%q", planner.lastReason, planner.lastActor)
	}
	if planner.lastStart.Format(time.RFC3339) != start || planner.lastEnd.Format(time.RFC3339) != end {
		t.Fatalf("起止 = %v ~ %v", planner.lastStart, planner.lastEnd)
	}
}

// 容量报告复用预览，但不下发行明细。
func TestCapacityReport(t *testing.T) {
	planner := &fakePlanner{
		preview: samplePreview(),
		task:    coreschedule.TaskInfo{ID: "t1", SchoolID: "s1"},
	}
	planner.preview.Demand = 100
	planner.preview.Shortfall = 36
	planner.preview.Binding = "workers"
	planner.preview.SuggestedWorkers = 4
	engine, issued := newRouter(t, corescope.Master(), planner, &fakeTimePrefs{})
	rec := do(t, engine, issued, http.MethodGet, "/api/capacity?school_id=s1&task_id=t1", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d: %s", rec.Code, rec.Body.String())
	}
	var raw map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &raw); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if _, ok := raw["rows"]; ok {
		t.Fatal("容量报告不应含行明细")
	}
	if raw["capacity"].(float64) != 64 || raw["demand"].(float64) != 100 {
		t.Fatalf("raw = %v", raw)
	}
	if raw["binding"] != "workers" || raw["suggested_workers"] != float64(4) {
		t.Fatalf("binding/suggested = %v/%v", raw["binding"], raw["suggested_workers"])
	}
	if raw["day"].(string) == "" {
		t.Fatal("缺省日期应补今天")
	}
	// k0y-4：容量报告走 Report，缺省不刷新（只读缓存）。
	if planner.reportCalls != 1 {
		t.Fatalf("容量报告应调用 Report：calls = %d", planner.reportCalls)
	}
	if planner.lastInput.Refresh {
		t.Fatal("缺省 refresh 必须为假：默认只读缓存")
	}

	// F5：GET 的 refresh 参数被忽略；强制刷新走 POST 变体。
	rec = do(t, engine, issued, http.MethodGet,
		"/api/capacity?school_id=s1&task_id=t1&refresh=true", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("refresh status = %d: %s", rec.Code, rec.Body.String())
	}
	if planner.lastInput.Refresh {
		t.Fatal("GET refresh 必须被忽略：GET 路径不得触发平台发现")
	}
	rec = do(t, engine, issued, http.MethodPost,
		"/api/capacity/refresh?school_id=s1&task_id=t1", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("POST refresh status = %d: %s", rec.Code, rec.Body.String())
	}
	if !planner.lastInput.Refresh {
		t.Fatalf("POST refresh 未透传：calls = %d refresh = %v",
			planner.reportCalls, planner.lastInput.Refresh)
	}

	// 建议值 0 表示不给：字段整体缺席，不写 0。
	planner.preview.SuggestedWorkers = 0
	rec = do(t, engine, issued, http.MethodGet, "/api/capacity?school_id=s1&task_id=t1", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("no suggestion status = %d: %s", rec.Code, rec.Body.String())
	}
	raw = nil
	if err := json.Unmarshal(rec.Body.Bytes(), &raw); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if _, ok := raw["suggested_workers"]; ok {
		t.Fatalf("建议值 0 必须省略: %v", raw)
	}
	if raw["binding"] != "workers" {
		t.Fatalf("binding = %v, want workers", raw["binding"])
	}
}

// 自选：任意角色都可读写，偏好行恒按会话 userID 归属；写入偏移原样传递。
func TestTimePrefRoutes(t *testing.T) {
	prefs := &fakeTimePrefs{view: apptimepref.View{OffsetMin: intPtr(30), Allowed: true}}
	engine, issued := newRouter(t, corescope.User("u1"), &fakePlanner{}, prefs)
	rec := do(t, engine, issued, http.MethodGet, "/api/me/time-pref", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("get status = %d: %s", rec.Code, rec.Body.String())
	}
	var got oapi.TimePref
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if got.OffsetMin == nil || *got.OffsetMin != 30 || !got.Allowed {
		t.Fatalf("body = %+v", got)
	}
	rec = do(t, engine, issued, http.MethodPut, "/api/me/time-pref", map[string]any{"offset_min": 45})
	if rec.Code != http.StatusOK || prefs.sets != 1 {
		t.Fatalf("put status = %d sets = %d: %s", rec.Code, prefs.sets, rec.Body.String())
	}
	if prefs.lastOffset == nil || *prefs.lastOffset != 45 {
		t.Fatalf("offset = %v", prefs.lastOffset)
	}

	// k0y-7：缺 offset_min 键必须 400，不得被当作清除（显式 null 才清除）。
	rec = do(t, engine, issued, http.MethodPut, "/api/me/time-pref", map[string]any{})
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("缺键 put status = %d, want 400: %s", rec.Code, rec.Body.String())
	}
	if prefs.sets != 1 {
		t.Fatalf("缺键不得调用 Set：sets = %d", prefs.sets)
	}
	// 显式 null 是合法清除：键在、值为 nil → 200 且 Set 收到 nil。
	rec = do(t, engine, issued, http.MethodPut, "/api/me/time-pref", map[string]any{"offset_min": nil})
	if rec.Code != http.StatusOK {
		t.Fatalf("显式 null put status = %d, want 200: %s", rec.Code, rec.Body.String())
	}
	if prefs.sets != 2 || prefs.lastOffset != nil {
		t.Fatalf("null 清除未生效：sets=%d lastOffset=%v", prefs.sets, prefs.lastOffset)
	}

	if prefs.lastGetUser != "u1" || prefs.lastSetUser != "u1" {
		t.Fatalf("偏好归属 = get:%q set:%q, want u1（会话 userID）", prefs.lastGetUser, prefs.lastSetUser)
	}

	// master 同样可读写，归属仍是自己的 userID（u1）。
	prefs.lastGetUser, prefs.lastSetUser = "", ""
	engine, issued = newRouter(t, corescope.Master(), &fakePlanner{}, prefs)
	rec = do(t, engine, issued, http.MethodGet, "/api/me/time-pref", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("master get status = %d, want 200", rec.Code)
	}
	rec = do(t, engine, issued, http.MethodPut, "/api/me/time-pref", map[string]any{"offset_min": 45})
	if rec.Code != http.StatusOK {
		t.Fatalf("master put status = %d, want 200", rec.Code)
	}
	if prefs.lastGetUser != "u1" || prefs.lastSetUser != "u1" {
		t.Fatalf("master 偏好归属 = get:%q set:%q, want u1", prefs.lastGetUser, prefs.lastSetUser)
	}
}

// 自选读面：有账号任务时带出任务窗口、拥挤度与台账预计时段；
// 无任务时保留标准窗口，estimated=null 且 note=待生成；task_id 不下发。
func TestTimePrefGetEnrichesCrowdingAndEstimate(t *testing.T) {
	runAt := time.Date(2026, 10, 29, 6, 42, 0, 0, shanghai)
	cachedStart := time.Date(2026, 10, 29, 7, 0, 0, 0, shanghai)
	planner := &fakePlanner{
		self: coreschedule.SelfContext{
			HasAccount: true, AccountID: "acct-1", SchoolID: "s1",
			HasTask: true, TaskID: "task-secret", RunAt: &runAt,
		},
		cached: coreschedule.WindowRecord{
			SchoolTaskID: "task-secret", Day: "2026-10-29",
			Start: cachedStart, End: cachedStart.Add(80 * time.Minute),
			Source: "platform", Version: 1,
		},
	}
	// prefs view 的标准窗口刻意与任务窗口不同：断言不能同义反复。
	prefs := &fakeTimePrefs{view: apptimepref.View{
		OffsetMin: intPtr(30), Allowed: true,
		Window: apptimepref.Window{Start: "06:30", End: "07:50"},
	}}
	stats := &fakeStats{counts: map[int]int{0: 3, 5: 10}}
	engine, issued := newRouterWithStats(t, corescope.User("u1"), planner, prefs, stats)

	rec := do(t, engine, issued, http.MethodGet, "/api/me/time-pref", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("get status = %d: %s", rec.Code, rec.Body.String())
	}
	var got oapi.TimePref
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if got.Window.Start != "07:00" || got.Window.End != "08:20" {
		t.Fatalf("window = %+v, want cached task window 07:00~08:20", got.Window)
	}
	if got.Slots == nil || len(*got.Slots) != 16 {
		t.Fatalf("slots = %v, want 16", got.Slots)
	}
	slots := *got.Slots
	if slots[0].OffsetMin != 0 || slots[0].Label != "07:00" || slots[0].Pct != 30 {
		t.Fatalf("首槽 = %+v, want 07:00 30%% (3/10)", slots[0])
	}
	if slots[1].Pct != 100 {
		t.Fatalf("满员槽 pct = %d, want 100 (10/10)", slots[1].Pct)
	}
	if slots[0].EdgeNote != "开头 0.5 分钟保留" {
		t.Fatalf("首槽 edge_note = %q", slots[0].EdgeNote)
	}
	if slots[len(slots)-1].EdgeNote != "结尾 0.5 分钟保留" {
		t.Fatalf("末槽 edge_note = %q", slots[len(slots)-1].EdgeNote)
	}
	// 拥挤度必须走任务级端口，并带上选中任务与业务日（P2-13）。
	if stats.taskCalls != 1 || stats.lastTask != "task-secret" || stats.lastDay != "2026-10-29" {
		t.Fatalf("任务级拥挤度 = calls %d task %q day %q, want 1/task-secret/2026-10-29",
			stats.taskCalls, stats.lastTask, stats.lastDay)
	}
	if got.Estimated == nil || *got.Estimated != "06:42" {
		t.Fatalf("estimated = %v, want 06:42", got.Estimated)
	}
	if !strings.Contains(got.EstimateNote, "非承诺") {
		t.Fatalf("estimate_note = %q, 必须写明非承诺", got.EstimateNote)
	}
	// 内部任务 ID 与学校 ID 都不得出现在用户响应里。
	if strings.Contains(rec.Body.String(), "task-secret") || strings.Contains(rec.Body.String(), `"s1"`) {
		t.Fatalf("响应泄露内部 ID: %s", rec.Body.String())
	}
	// 用户读面不得走 Report/平台发现；只走缓存窗口。
	if planner.reportCalls != 0 || planner.cachedCalls != 1 {
		t.Fatalf("report=%d cached=%d, want 0/1", planner.reportCalls, planner.cachedCalls)
	}
	if planner.lastCachedIn.SchoolID != "s1" || planner.lastCachedIn.SchoolTaskID != "task-secret" {
		t.Fatalf("cached 输入 = %+v", planner.lastCachedIn)
	}
	if planner.selfUserID != "u1" || planner.selfDay != "2026-10-29" {
		t.Fatalf("self 输入 = %q %q", planner.selfUserID, planner.selfDay)
	}
}

// 无任务上下文时不得调用 Report/stats：window 走标准窗口，estimated=null。
func TestTimePrefGetWithoutTaskKeepsStandardWindow(t *testing.T) {
	planner := &fakePlanner{self: coreschedule.SelfContext{HasAccount: true, AccountID: "acct-1", SchoolID: "s1"}}
	prefs := &fakeTimePrefs{view: apptimepref.View{
		Allowed: true,
		Window:  apptimepref.Window{Start: "06:30", End: "07:50"},
		Slots:   []apptimepref.Slot{{OffsetMin: 0, Label: "06:30"}},
	}}
	engine, issued := newRouterWithStats(t, corescope.User("u1"), planner, prefs, &fakeStats{})
	rec := do(t, engine, issued, http.MethodGet, "/api/me/time-pref", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("get status = %d: %s", rec.Code, rec.Body.String())
	}
	var got oapi.TimePref
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if got.Window.Start != "06:30" || got.Window.End != "07:50" {
		t.Fatalf("window = %+v, want standard window", got.Window)
	}
	if got.Estimated != nil {
		t.Fatalf("estimated = %v, want null", *got.Estimated)
	}
	if got.EstimateNote != "待生成" {
		t.Fatalf("estimate_note = %q, want 待生成", got.EstimateNote)
	}
	if got.Slots == nil || len(*got.Slots) != 1 || (*got.Slots)[0].Pct != 0 {
		t.Fatalf("slots = %v, want one slot with pct 0", got.Slots)
	}
	if planner.reportCalls != 0 || planner.cachedCalls != 0 {
		t.Fatalf("无任务不得读窗口：report=%d cached=%d", planner.reportCalls, planner.cachedCalls)
	}
}

// 缺 stats 时在读账号/窗口之前失败；用户读面错误来源单一。
func TestTimePrefGetWithoutStatsFailsBeforeWindowRead(t *testing.T) {
	planner := &fakePlanner{self: coreschedule.SelfContext{
		HasAccount: true, SchoolID: "s1", HasTask: true, TaskID: "task-secret",
	}}
	prefs := &fakeTimePrefs{view: apptimepref.View{Allowed: true}}
	engine, issued := newRouterFull(t, corescope.User("u1"), planner, prefs, nil, nil)
	rec := do(t, engine, issued, http.MethodGet, "/api/me/time-pref", nil)
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500: %s", rec.Code, rec.Body.String())
	}
	if planner.cachedCalls != 0 || planner.reportCalls != 0 {
		t.Fatalf("缺 stats 不得读窗口：cached=%d report=%d", planner.cachedCalls, planner.reportCalls)
	}
}

// PUT 写入成功后窗口读面失败：200 基础视图＋结构化日志，不把写报成失败。
func TestTimePrefPutLogsEnrichmentFailure(t *testing.T) {
	var logs bytes.Buffer
	logger := slog.New(slog.NewTextHandler(&logs, nil))
	planner := &fakePlanner{
		self: coreschedule.SelfContext{
			HasAccount: true, SchoolID: "s1", HasTask: true, TaskID: "task-secret",
		},
		cachedErr: errs.New(errs.CodeInternal, "window cache read failed"),
	}
	prefs := &fakeTimePrefs{view: apptimepref.View{
		Allowed: true,
		Window:  apptimepref.Window{Start: "06:30", End: "07:50"},
		Slots:   []apptimepref.Slot{{OffsetMin: 0, Label: "06:30"}},
	}}
	engine, issued := newRouterFull(t, corescope.User("u1"), planner, prefs, &fakeStats{}, logger)
	rec := do(t, engine, issued, http.MethodPut, "/api/me/time-pref", map[string]any{"offset_min": 45})
	if rec.Code != http.StatusOK {
		t.Fatalf("put status = %d, want 200: %s", rec.Code, rec.Body.String())
	}
	if prefs.sets != 1 {
		t.Fatalf("写未执行：sets = %d", prefs.sets)
	}
	var got oapi.TimePref
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if got.Window.Start != "06:30" || got.Window.End != "07:50" {
		t.Fatalf("降级视图 window = %+v, want 标准窗口", got.Window)
	}
	if got.Estimated != nil || got.EstimateNote != "待生成" {
		t.Fatalf("降级视图 estimated = %v note = %q", got.Estimated, got.EstimateNote)
	}
	logText := logs.String()
	if !strings.Contains(logText, "time_pref.enrich_failed") {
		t.Fatalf("缺降级事件日志: %q", logText)
	}
	if !strings.Contains(logText, "user_id=u1") ||
		!strings.Contains(logText, "school_task_id=task-secret") {
		t.Fatalf("日志缺 user/task 键: %q", logText)
	}
}

// 用户读面按计划实际片数，不套 admin stats 的两小时上限。
func TestTimePrefSlotsFollowPlanBeyondTwoHours(t *testing.T) {
	start := time.Date(2026, 10, 29, 6, 30, 0, 0, shanghai)
	planner := &fakePlanner{
		self: coreschedule.SelfContext{
			HasAccount: true, SchoolID: "s1", HasTask: true, TaskID: "task-secret",
		},
		cached: coreschedule.WindowRecord{Start: start, End: start.Add(3 * time.Hour)},
	}
	prefs := &fakeTimePrefs{view: apptimepref.View{Allowed: true}}
	engine, issued := newRouterWithStats(t, corescope.User("u1"), planner, prefs, &fakeStats{})
	rec := do(t, engine, issued, http.MethodGet, "/api/me/time-pref", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d: %s", rec.Code, rec.Body.String())
	}
	var got oapi.TimePref
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if got.Slots == nil || len(*got.Slots) != 36 {
		t.Fatalf("slots = %v, want 36 for a 3h window", got.Slots)
	}
}

// 冷却错误映射 429。
func TestTimePrefRateLimited(t *testing.T) {
	prefs := &fakeTimePrefs{err: errs.New(errs.CodeRateLimited, "too recent")}
	engine, issued := newRouter(t, corescope.User("u1"), &fakePlanner{}, prefs)
	rec := do(t, engine, issued, http.MethodPut, "/api/me/time-pref", map[string]any{"offset_min": 45})
	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("status = %d, want 429: %s", rec.Code, rec.Body.String())
	}
}

func intPtr(v int) *int { return &v }

// 普通用户不得改窗口：403，且不得调用用例。
func TestOverrideUserForbidden(t *testing.T) {
	planner := &fakePlanner{preview: samplePreview()}
	engine, issued := newRouter(t, corescope.User("u1"), planner, &fakeTimePrefs{})
	body := map[string]string{
		"day": "2026-10-29", "school_id": "s1", "task_id": "t1",
		"start": "2026-10-29T09:00:00+08:00", "end": "2026-10-29T09:30:00+08:00",
		"reason": "调整",
	}
	rec := do(t, engine, issued, http.MethodPost, "/api/schedule/override", body)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403: %s", rec.Code, rec.Body.String())
	}
	if planner.lastReason != "" {
		t.Fatal("无权重排后不得调用用例")
	}
}

// fakeOverrides 是每请求覆盖链替身：周末门与 sign_dist 可注入。
type fakeOverrides struct {
	usePrefs      bool
	front         time.Duration
	back          time.Duration
	signDist      appschedule.SignDist
	signDistCalls int
	weekend       map[string]bool
	weekendCalls  []string
}

func (f *fakeOverrides) UsePrefs(context.Context) (bool, error) { return f.usePrefs, nil }

func (f *fakeOverrides) Edges(context.Context) (time.Duration, time.Duration, error) {
	return f.front, f.back, nil
}

func (f *fakeOverrides) SignDist(context.Context) (appschedule.SignDist, error) {
	f.signDistCalls++
	return f.signDist, nil
}

func (f *fakeOverrides) WeekendSign(_ context.Context, day time.Time) (bool, error) {
	key := day.Format("2006-01-02")
	f.weekendCalls = append(f.weekendCalls, key)
	if v, ok := f.weekend[key]; ok {
		return v, nil
	}
	return true, nil
}

// 周六门关闭：POST plan 成功但 0 行 + note，且不调用 Build（P1-2/P1-3）。
func TestPlanWeekendGateClosedReturnsZeroRows(t *testing.T) {
	planner := &fakePlanner{
		preview: samplePreview(),
		task:    coreschedule.TaskInfo{ID: "t1", SchoolID: "s1", Name: "晨签", Enabled: true},
	}
	ov := &fakeOverrides{weekend: map[string]bool{"2026-10-31": false}}
	saturday := time.Date(2026, 10, 31, 8, 0, 0, 0, shanghai)
	engine, issued := newRouterAt(t, corescope.Master(), planner, &fakeTimePrefs{}, nil, nil, saturday, ov)

	rec := do(t, engine, issued, http.MethodPost, "/api/schedule/plan", map[string]string{
		"day": "2026-10-31", "school_id": "s1", "task_id": "t1",
	})
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200: %s", rec.Code, rec.Body.String())
	}
	var body oapi.SchedulePlan
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(body.Rows) != 0 || body.Demand != 0 || len(body.Histogram) != 0 {
		t.Fatalf("周末门关闭必须 0 行: %+v", body)
	}
	if body.Note == nil || *body.Note == "" {
		t.Fatalf("周末门关闭必须带说明回执: %+v", body)
	}
	if planner.builds != 1 {
		t.Fatalf("周末门关闭应调用一次 Build（校验链先跑）: %d", planner.builds)
	}
	if !planner.lastInput.WeekendClosed {
		t.Fatal("周末门关闭未透传到用例层")
	}
	if body.Source != "weekend_closed" {
		t.Fatalf("回执来源 = %s, want weekend_closed", body.Source)
	}
	if len(ov.weekendCalls) != 1 || ov.weekendCalls[0] != "2026-10-31" {
		t.Fatalf("周末门解析日 = %v, want [2026-10-31]", ov.weekendCalls)
	}
}

// 周末门开启：照常 Build；sign_dist 每请求从覆盖链读。
func TestPlanUsesResolvedSignDist(t *testing.T) {
	planner := &fakePlanner{
		preview: samplePreview(),
		task:    coreschedule.TaskInfo{ID: "t1", SchoolID: "s1", Name: "晨签", Enabled: true},
	}
	ov := &fakeOverrides{signDist: appschedule.SignDistUniform, weekend: map[string]bool{"2026-10-29": true}}
	engine, issued := newRouterAt(t, corescope.Master(), planner, &fakeTimePrefs{}, nil, nil,
		time.Date(2026, 10, 29, 8, 0, 0, 0, shanghai), ov)

	rec := do(t, engine, issued, http.MethodPost, "/api/schedule/plan", map[string]string{
		"day": "2026-10-29", "school_id": "s1", "task_id": "t1",
	})
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200: %s", rec.Code, rec.Body.String())
	}
	if planner.builds != 1 {
		t.Fatalf("Build calls = %d, want 1", planner.builds)
	}
	if planner.lastInput.SignDist != appschedule.SignDistUniform {
		t.Fatalf("SignDist = %v, want uniform（每请求读覆盖链）", planner.lastInput.SignDist)
	}
	if ov.signDistCalls != 1 {
		t.Fatalf("sign_dist 解析次数 = %d, want 1", ov.signDistCalls)
	}
}

// 周六门关闭：POST override 成功但 0 行 + note，且不写覆盖。
func TestOverrideWeekendGateClosedSkipsWrite(t *testing.T) {
	planner := &fakePlanner{
		preview: samplePreview(),
		task:    coreschedule.TaskInfo{ID: "t1", SchoolID: "s1", Name: "晨签", Enabled: true},
	}
	ov := &fakeOverrides{weekend: map[string]bool{"2026-10-31": false}}
	engine, issued := newRouterAt(t, corescope.Master(), planner, &fakeTimePrefs{}, nil, nil,
		time.Date(2026, 10, 31, 8, 0, 0, 0, shanghai), ov)

	rec := do(t, engine, issued, http.MethodPost, "/api/schedule/override", map[string]string{
		"day": "2026-10-31", "school_id": "s1", "task_id": "t1",
		"start": "2026-10-31T09:00:00+08:00", "end": "2026-10-31T09:30:00+08:00",
		"reason": "调整",
	})
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200: %s", rec.Code, rec.Body.String())
	}
	var body oapi.SchedulePlan
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(body.Rows) != 0 || body.Note == nil || *body.Note == "" {
		t.Fatalf("周末门关闭必须 0 行 + note: %+v", body)
	}
	if body.Source != "weekend_closed" {
		t.Fatalf("回执来源 = %s, want weekend_closed（不回显请求窗口）", body.Source)
	}
	if planner.overrides != 1 {
		t.Fatalf("周末门关闭应调用一次 Override（校验链先跑）: %d", planner.overrides)
	}
	if !planner.lastInput.WeekendClosed {
		t.Fatal("周末门关闭未透传到用例层")
	}
}

// 周六门关闭：GET 预览也回 0 行 + note，且不发现平台。
func TestPreviewWeekendGateClosedReturnsZeroRows(t *testing.T) {
	planner := &fakePlanner{
		preview: samplePreview(),
		task:    coreschedule.TaskInfo{ID: "t1", SchoolID: "s1", Name: "晨签", Enabled: true},
	}
	ov := &fakeOverrides{weekend: map[string]bool{"2026-10-31": false}}
	engine, issued := newRouterAt(t, corescope.Master(), planner, &fakeTimePrefs{}, nil, nil,
		time.Date(2026, 10, 31, 8, 0, 0, 0, shanghai), ov)

	rec := do(t, engine, issued, http.MethodGet,
		"/api/schedule/preview?day=2026-10-31&school_id=s1&task_id=t1", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200: %s", rec.Code, rec.Body.String())
	}
	var body oapi.SchedulePlan
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(body.Rows) != 0 || body.Note == nil || *body.Note == "" {
		t.Fatalf("周末门关闭必须 0 行 + note: %+v", body)
	}
	if planner.previewCalls != 0 {
		t.Fatalf("周末门关闭不得走实时预览: live calls = %d", planner.previewCalls)
	}
	if planner.previewCachedCalls != 1 || !planner.lastInput.WeekendClosed {
		t.Fatalf("周末门关闭应走一次只读预览并透传 WeekendClosed: cached=%d closed=%v",
			planner.previewCachedCalls, planner.lastInput.WeekendClosed)
	}
	if body.Source != "weekend_closed" {
		t.Fatalf("回执来源 = %s, want weekend_closed", body.Source)
	}
}
