package router_test

// 调度设置写回端到端（sett2）：真路由、真用例链。
//
// 断言用户可见不变式（写面咬接线）：
//  1. PUT /api/admin/schedule-settings 开自选开关 → GET /api/me/time-pref
//     立即报 allowed=true，PUT /api/me/time-pref 可以写入（闸开）。
//  2. 显式 null 删行 → 闸立即关闭：GET 报 allowed=false，写回 403。
//  3. 缺 CSRF → 403；user 写调度设置 → 403。
//
// 用例链全真（appsettings.Service/Resolver/Roster、apptimepref.Service），
// 只替持久化与平台面：覆盖行存取、审计、事务、自选存储、排期用例。

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schedule"
	appsettings "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/settings"
	appstats "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/stats"
	apptimepref "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/timepref"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/router"
	schedulehttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/schedule"
	systemhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/system"
)

// e2eEnvSource 是固定环境面：本 e2e 不设置任何 env 键（全走 default）。
type e2eEnvSource map[string]string

func (m e2eEnvSource) Lookup(key string) (string, bool) {
	value, ok := m[key]
	return value, ok
}

// e2eOverrideStore 是覆盖行存取的内存替身（sett2 三键）。
type e2eOverrideStore struct {
	intRows  map[string]string
	boolRows map[string]string
	strRows  map[string]string
}

func newE2EOverrideStore() *e2eOverrideStore {
	return &e2eOverrideStore{
		intRows: map[string]string{}, boolRows: map[string]string{}, strRows: map[string]string{},
	}
}

func (s *e2eOverrideStore) IntOverride(_ context.Context, key string) (int, bool, error) {
	raw, ok := s.intRows[key]
	if !ok {
		return 0, false, nil
	}
	value, err := strconv.Atoi(raw)
	if err != nil {
		return 0, true, coreerrs.New(coreerrs.CodeValidation, "settings value is not an integer")
	}
	return value, true, nil
}

func (s *e2eOverrideStore) BoolOverride(_ context.Context, key string) (bool, bool, error) {
	raw, ok := s.boolRows[key]
	if !ok {
		return false, false, nil
	}
	switch raw {
	case "true":
		return true, true, nil
	case "false":
		return false, true, nil
	default:
		return false, true, coreerrs.New(coreerrs.CodeValidation, "settings value is not a boolean literal")
	}
}

func (s *e2eOverrideStore) SetInt(_ context.Context, key string, value int, _ string) error {
	s.intRows[key] = strconv.Itoa(value)
	return nil
}

func (s *e2eOverrideStore) SetBool(_ context.Context, key string, value bool, _ string) error {
	s.boolRows[key] = strconv.FormatBool(value)
	return nil
}

func (s *e2eOverrideStore) StringOverride(_ context.Context, key string) (string, bool, error) {
	raw, ok := s.strRows[key]
	return raw, ok, nil
}

func (s *e2eOverrideStore) SetString(_ context.Context, key, value, _ string) error {
	s.strRows[key] = value
	return nil
}

func (s *e2eOverrideStore) ClearSetting(_ context.Context, key string) error {
	delete(s.intRows, key)
	delete(s.boolRows, key)
	delete(s.strRows, key)
	return nil
}

// e2eAudit 是审计的内存替身。
type e2eAudit struct{ entries []coreaudit.Entry }

func (m *e2eAudit) Append(_ context.Context, in coreaudit.Entry) (coreaudit.Row, error) {
	m.entries = append(m.entries, in)
	return coreaudit.Row{}, nil
}

func (m *e2eAudit) Verify(context.Context, int64, int64) (coreaudit.Report, error) {
	return coreaudit.Report{}, nil
}

// e2eTx 直通事务。
type e2eTx struct{}

func (e2eTx) InTx(ctx context.Context, fn func(context.Context) error) error { return fn(ctx) }

// e2ePrefStore 是自选存储的内存替身：无记录即 ErrNotFound。
type e2ePrefStore struct{ puts int }

func (s *e2ePrefStore) Get(context.Context, string) (coreschedule.TimePref, error) {
	return coreschedule.TimePref{}, coreerrs.ErrNotFound
}

func (s *e2ePrefStore) PutWithin(_ context.Context, _ string, _ *int, _ time.Time, _ time.Duration) (bool, error) {
	s.puts++
	return true, nil
}

// e2ePlanner 只实现自选读面需要的最小排期面：无任务、标准窗口。
type e2ePlanner struct{}

func (e2ePlanner) Preview(context.Context, appschedule.Input) (appschedule.Preview, error) {
	return appschedule.Preview{}, nil
}

func (e2ePlanner) Build(context.Context, appschedule.Input) (appschedule.Preview, error) {
	return appschedule.Preview{}, nil
}

func (e2ePlanner) Override(context.Context, corescope.Scope, appschedule.Input,
	time.Time, time.Time, string, string) (appschedule.Preview, error) {
	return appschedule.Preview{}, nil
}

func (e2ePlanner) Report(context.Context, appschedule.Input) (appschedule.Preview, error) {
	return appschedule.Preview{}, nil
}

func (e2ePlanner) Task(context.Context, corescope.Scope, string) (coreschedule.TaskInfo, error) {
	return coreschedule.TaskInfo{}, coreerrs.ErrNotFound
}

func (e2ePlanner) ListWindows(context.Context, corescope.Scope, string, bool) ([]coreschedule.WindowView, error) {
	return nil, nil
}

func (e2ePlanner) SelfContext(context.Context, string, string) (coreschedule.SelfContext, error) {
	return coreschedule.SelfContext{}, nil
}

func (e2ePlanner) CachedWindow(context.Context, appschedule.Input) (coreschedule.WindowRecord, error) {
	return coreschedule.WindowRecord{}, coreerrs.ErrNotFound
}

// e2eAuthorizer 把会话用户映射成作用域。
type e2eAuthorizer struct{}

func (e2eAuthorizer) Authorize(_ context.Context, user coreauth.User) (corescope.Scope, appaudit.Actor, error) {
	switch user.Role {
	case coreauth.RoleMaster:
		return corescope.Master(), appaudit.Actor{UserID: user.ID, Role: user.Role}, nil
	case coreauth.RoleAdmin:
		return corescope.Admin(nil), appaudit.Actor{UserID: user.ID, Role: user.Role}, nil
	default:
		return corescope.User(user.ID), appaudit.Actor{UserID: user.ID, Role: user.Role}, nil
	}
}

// e2eStats 是看板聚合的内存替身（读面只取 OffsetCounts）。
type e2eStats struct{}

func (e2eStats) SignStats(context.Context, corescope.Scope, string, string) (appstats.Report, error) {
	return appstats.Report{}, nil
}

func (e2eStats) OffsetCounts(context.Context, string) (map[int]int, error) {
	return map[int]int{}, nil
}

// e2eAnnouncementStore 是公告存储的内存替身（设置用例的必填依赖）。
type e2eAnnouncementStore struct{}

func (e2eAnnouncementStore) Announcement(context.Context) (string, *time.Time, error) {
	return "", nil, nil
}

func (e2eAnnouncementStore) SetAnnouncement(context.Context, string, string) (*time.Time, error) {
	return nil, nil
}

// settingsOverrideAdapter 把解析器接到 schedulehttp.Overrides 端口。
type settingsOverrideAdapter struct {
	resolver *appsettings.Resolver
}

func (a settingsOverrideAdapter) UsePrefs(ctx context.Context) (bool, error) {
	return a.resolver.ResolveBool(ctx, appsettings.KeyScheduleAllowTimePref, false)
}

func (a settingsOverrideAdapter) Edges(ctx context.Context) (time.Duration, time.Duration, error) {
	front, err := a.resolver.ResolveInt(ctx, appsettings.KeyScheduleEdgeFrontSec, 60)
	if err != nil {
		return 0, 0, err
	}
	back, err := a.resolver.ResolveInt(ctx, appsettings.KeyScheduleEdgeBackSec, 60)
	if err != nil {
		return 0, 0, err
	}
	return time.Duration(front) * time.Second, time.Duration(back) * time.Second, nil
}

// newScheduleSettingsEngine 构造真用例链的完整路由引擎。
func newScheduleSettingsEngine(t *testing.T) (*gin.Engine, *stubIssuer, *e2eOverrideStore) {
	t.Helper()
	store := newE2EOverrideStore()
	resolver, err := appsettings.NewResolver(store, map[string]appsettings.SettingDomain{
		appsettings.KeyScheduleEdgeFrontSec: {Min: 0, Max: 300},
		appsettings.KeyScheduleEdgeBackSec:  {Min: 0, Max: 300},
	}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatalf("NewResolver: %v", err)
	}
	roster, err := appsettings.NewRoster([]appsettings.Entry{
		{Key: "YIBAN_WINDOW_EDGE_FRONT_SEC", Group: "调度", Default: float64(60), HasDefault: true},
		{Key: "YIBAN_WINDOW_EDGE_BACK_SEC", Group: "调度", Default: float64(60), HasDefault: true},
		{Key: "YIBAN_ALLOW_TIME_PREF", Group: "调度", Default: false, HasDefault: true},
	}, e2eEnvSource{})
	if err != nil {
		t.Fatalf("NewRoster: %v", err)
	}
	roster.WithOverrides(resolver)
	audits := &e2eAudit{}
	now := coreclock.Fixed{T: time.Date(2026, 11, 10, 9, 0, 0, 0, time.UTC)}
	settingsSvc, err := appsettings.NewService(appsettings.Deps{
		Store: e2eAnnouncementStore{}, Audit: audits, Tx: e2eTx{}, Clock: now,
		Schedule: store, Resolver: resolver,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	systemHandler := systemhttp.NewHandler(roster, settingsSvc, nil).
		WithAutoApprove(settingsSvc).
		WithScheduleSettings(settingsSvc)

	prefSvc, err := apptimepref.NewService(apptimepref.Deps{
		Store: &e2ePrefStore{}, Clock: now,
		Allow: func(ctx context.Context) (bool, error) {
			return resolver.ResolveBool(ctx, appsettings.KeyScheduleAllowTimePref, false)
		},
	})
	if err != nil {
		t.Fatalf("NewService(timepref): %v", err)
	}
	scheduleHandler := schedulehttp.NewHandler(e2ePlanner{}, prefSvc, e2eAuthorizer{}, schedulehttp.Config{
		Location: time.UTC, Clock: now,
		Overrides: settingsOverrideAdapter{resolver: resolver},
	}).WithStats(e2eStats{})

	issuer := newStubIssuer()
	gin.SetMode(gin.TestMode)
	engine, err := router.New(router.Options{
		Sessions: issuer,
		System:   systemHandler,
		Schedule: scheduleHandler,
		Logger:   slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if err != nil {
		t.Fatalf("router.New: %v", err)
	}
	return engine, issuer, store
}

// PUT 开闸 → GET/PUT /api/me/time-pref 立即可用；null 删行 → 立即关闭。
func TestScheduleSettingsHotGateEndToEnd(t *testing.T) {
	engine, issuer, store := newScheduleSettingsEngine(t)
	master := issueUser(t, issuer, coreauth.RoleMaster)
	user := issueUser(t, issuer, coreauth.RoleUser)

	putSchedule := func(body, csrf string, session *coreauth.Session) *httptest.ResponseRecorder {
		return serve(engine, requestWithSession(http.MethodPut, "/api/admin/schedule-settings", body, csrf, session))
	}

	// 角色负例与 CSRF 负例先行：user 写 403；master 缺 CSRF 403。
	if rec := putSchedule(`{"allow_time_pref":true}`, "", &master); rec.Code != http.StatusForbidden {
		t.Fatalf("missing csrf = %d, want 403: %s", rec.Code, rec.Body.String())
	}
	if rec := putSchedule(`{"allow_time_pref":true}`, master.CSRFToken, &user); rec.Code != http.StatusForbidden {
		t.Fatalf("user put = %d, want 403", rec.Code)
	}
	if rows := len(store.boolRows); rows != 0 {
		t.Fatalf("被拒写落了覆盖行: %v", store.boolRows)
	}

	// 开闸：DB 行 true 覆盖 env（env 未设为 false），下一次请求立即生效。
	if rec := putSchedule(`{"allow_time_pref":true}`, master.CSRFToken, &master); rec.Code != http.StatusOK {
		t.Fatalf("open gate = %d: %s", rec.Code, rec.Body.String())
	}
	rec := serve(engine, requestWithSession(http.MethodGet, "/api/me/time-pref", "", "", &user))
	if rec.Code != http.StatusOK {
		t.Fatalf("get time-pref = %d: %s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), `"allowed":true`) {
		t.Fatalf("gate did not open for next request: %s", rec.Body.String())
	}
	rec = serve(engine, requestWithSession(http.MethodPut, "/api/me/time-pref", `{"offset_min":30}`, user.CSRFToken, &user))
	if rec.Code != http.StatusOK {
		t.Fatalf("put time-pref with gate open = %d: %s", rec.Code, rec.Body.String())
	}

	// 删行回 env：闸立即关闭，写入 403。
	if rec := putSchedule(`{"allow_time_pref":null}`, master.CSRFToken, &master); rec.Code != http.StatusOK {
		t.Fatalf("clear gate = %d: %s", rec.Code, rec.Body.String())
	}
	rec = serve(engine, requestWithSession(http.MethodGet, "/api/me/time-pref", "", "", &user))
	if !strings.Contains(rec.Body.String(), `"allowed":false`) {
		t.Fatalf("gate did not close after clear: %s", rec.Body.String())
	}
	rec = serve(engine, requestWithSession(http.MethodPut, "/api/me/time-pref", `{"offset_min":30}`, user.CSRFToken, &user))
	if rec.Code != http.StatusForbidden {
		t.Fatalf("put time-pref with gate closed = %d, want 403: %s", rec.Code, rec.Body.String())
	}
}
