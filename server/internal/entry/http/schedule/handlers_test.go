package schedule_test

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schedule"
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
	lastInput   appschedule.Input
	lastActor   string
	lastStart   time.Time
	lastEnd     time.Time
	lastReason  string
	overrideErr error
}

func (f *fakePlanner) Preview(_ context.Context, in appschedule.Input) (appschedule.Preview, error) {
	f.lastInput = in
	return f.preview, f.previewErr
}

func (f *fakePlanner) Build(_ context.Context, in appschedule.Input) (appschedule.Preview, error) {
	f.builds++
	f.lastInput = in
	return f.preview, f.buildErr
}

func (f *fakePlanner) Override(_ context.Context, _ corescope.Scope, in appschedule.Input,
	start, end time.Time, reason, actorID string) (appschedule.Preview, error) {
	f.lastInput, f.lastStart, f.lastEnd = in, start, end
	f.lastReason, f.lastActor = reason, actorID
	return f.preview, f.overrideErr
}

func (f *fakePlanner) Task(context.Context, corescope.Scope, string) (coreschedule.TaskInfo, error) {
	return f.task, f.taskErr
}

func (f *fakePlanner) ListWindows(context.Context, corescope.Scope, string) ([]coreschedule.WindowView, error) {
	return f.views, f.listErr
}

type fakeTimePrefs struct {
	view       apptimepref.View
	err        error
	lastOffset *int
	sets       int
}

func (f *fakeTimePrefs) Get(context.Context, string) (apptimepref.View, error) {
	return f.view, f.err
}

func (f *fakeTimePrefs) Set(_ context.Context, _ string, offset *int) (apptimepref.View, error) {
	f.sets++
	f.lastOffset = offset
	return f.view, f.err
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
	issuer := &fakeIssuer{secret: []byte("0123456789abcdef0123456789abcdef")}
	issued, err := issuer.Issue(context.Background(), coreauth.User{
		ID: "u1", Email: "boss@example.com", Role: coreauth.RoleMaster, Status: coreauth.StatusActive,
	})
	if err != nil {
		t.Fatalf("issue session: %v", err)
	}
	handler := schedulehttp.NewHandler(planner, prefs, &fakeAuthorizer{scope: scope}, schedulehttp.Config{
		Location: shanghai, Clock: coreclock.Fixed{T: time.Date(2026, 10, 29, 8, 0, 0, 0, shanghai)}, Workers: 6, Avg: 3 * time.Second, Rate: 1.0, Util: 0.8,
		SlotsPerSlice: 10, Front: true, UsePrefs: true,
		EdgeFront: 30 * time.Second, EdgeBack: 30 * time.Second,
	})
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
	if raw["capacity"].(float64) != 64 || raw["demand"].(float64) != 1 {
		t.Fatalf("raw = %v", raw)
	}
	if raw["day"].(string) == "" {
		t.Fatal("缺省日期应补今天")
	}
}

// 自选：user 可读写；master 403；写入偏移原样传递。
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

	engine, issued = newRouter(t, corescope.Master(), &fakePlanner{}, prefs)
	rec = do(t, engine, issued, http.MethodGet, "/api/me/time-pref", nil)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("master status = %d, want 403", rec.Code)
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
