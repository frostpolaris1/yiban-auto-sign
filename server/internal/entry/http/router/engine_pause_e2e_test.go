package router_test

// 全局暂停端点（PUT /api/admin/engine/pause，批 3a）端到端：真路由、
// 真中间件、真用例链（appsettings.Service）。
//
// 断言用户可见不变式（写面咬接线）：
//  1. 缺 CSRF → 403；user 角色 → 403（均零写入零审计）。
//  2. admin 暂停（正确口令）→ 200 {"paused":true}；GET /api/admin/engine
//     快照 paused=true（热生效，无需重启）。
//  3. 方向分闸：admin 恢复 → 403 零写入；master 恢复 → 200，
//     快照回 paused=false。
//  4. current_password：缺失 400、错误 403，均零写入零审计行。

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	appschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schedule"
	appsettings "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/settings"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/router"
	schedulehttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/schedule"
	systemhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/system"
)

// e2eEngineSource 是执行监视快照的空替身：快照内容不是本文件判据。
type e2eEngineSource struct{}

func (e2eEngineSource) EngineSnapshot(context.Context, corescope.Scope, string, time.Time) (coreschedule.EngineData, error) {
	return coreschedule.EngineData{}, nil
}

// e2eReauth 是二次口令替身：只认 right-pass（替换口令占位，非真实凭据）。
type e2eReauth struct {
	rejected bool
}

func (r *e2eReauth) Reauthenticate(_ context.Context, _ string, password string) error {
	if password != "right-pass" || r.rejected {
		return errs.New(errs.CodeForbidden, "reauthentication failed")
	}
	return nil
}

// newEnginePauseEngine 构造真用例链的完整路由引擎。
func newEnginePauseEngine(t *testing.T) (*gin.Engine, *stubIssuer, *e2eOverrideStore, *e2eAudit) {
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
	}, e2eEnvSource{})
	if err != nil {
		t.Fatalf("NewRoster: %v", err)
	}
	roster.WithOverrides(resolver)
	audits := &e2eAudit{}
	settingsSvc, err := appsettings.NewService(appsettings.Deps{
		Store: e2eAnnouncementStore{}, Audit: audits, Tx: e2eTx{},
		Clock:    coreclock.Fixed{T: time.Date(2026, 11, 10, 9, 0, 0, 0, time.UTC)},
		Schedule: store, Resolver: resolver,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	systemHandler := systemhttp.NewHandler(roster, settingsSvc, nil)

	engineState := appworkerNewEngineState()
	engineHandler := schedulehttp.NewEngineHandler(
		appschedule.NewEngineQuery(e2eEngineSource{}, coreclock.Fixed{T: time.Date(2026, 11, 10, 9, 0, 0, 0, time.UTC)}, time.UTC),
		engineState, e2eAuthorizer{}).WithPauseGate(settingsSvc, &e2eReauth{})

	issuer := newStubIssuer()
	gin.SetMode(gin.TestMode)
	engine, err := router.New(router.Options{
		Sessions: issuer,
		System:   systemHandler,
		Engine:   engineHandler,
		Logger:   slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if err != nil {
		t.Fatalf("router.New: %v", err)
	}
	return engine, issuer, store, audits
}

// appworkerNewEngineState 是 app/worker.EngineState 的进程时刻替身。
func appworkerNewEngineState() *engineStateStub { return &engineStateStub{} }

type engineStateStub struct{}

func (*engineStateStub) EngineTimes() (*time.Time, *time.Time, *time.Time) {
	return nil, nil, nil
}

// putPause 以指定会话与 CSRF 发一次暂停写请求。
func putPause(body, csrf string, session *coreauth.Session) func(*gin.Engine) *httptest.ResponseRecorder {
	return func(engine *gin.Engine) *httptest.ResponseRecorder {
		return serve(engine, requestWithSession(http.MethodPut, "/api/admin/engine/pause", body, csrf, session))
	}
}

func getEngine(engine *gin.Engine, session *coreauth.Session) *httptest.ResponseRecorder {
	return serve(engine, requestWithSession(http.MethodGet, "/api/admin/engine", "", "", session))
}

// 全链路：CSRF/角色负例 → admin 暂停热生效 → 方向分闸 → master 恢复回 false。
func TestEnginePauseEndToEnd(t *testing.T) {
	engine, issuer, store, audits := newEnginePauseEngine(t)
	master := issueUser(t, issuer, coreauth.RoleMaster)
	admin := issueUser(t, issuer, coreauth.RoleAdmin)
	user := issueUser(t, issuer, coreauth.RoleUser)

	// CSRF 负例：缺令牌 403，零写入零审计。
	if rec := putPause(`{"paused":true,"current_password":"right-pass"}`, "", &admin)(engine); rec.Code != http.StatusForbidden {
		t.Fatalf("missing csrf = %d, want 403: %s", rec.Code, rec.Body.String())
	}
	// 角色负例：user 暂停 403。
	if rec := putPause(`{"paused":true,"current_password":"right-pass"}`, user.CSRFToken, &user)(engine); rec.Code != http.StatusForbidden {
		t.Fatalf("user pause = %d, want 403", rec.Code)
	}
	if len(store.boolRows) != 0 || len(audits.entries) != 0 {
		t.Fatalf("被拒请求落了行: rows=%v audits=%d", store.boolRows, len(audits.entries))
	}

	// admin 暂停成功：200 回显 true，GET 快照立即 paused=true。
	rec := putPause(`{"paused":true,"current_password":"right-pass"}`, admin.CSRFToken, &admin)(engine)
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `"paused":true`) {
		t.Fatalf("admin pause = %d: %s", rec.Code, rec.Body.String())
	}
	if len(audits.entries) != 1 || audits.entries[0].Action != coreaudit.ActionSettingsEnginePaused {
		t.Fatalf("audit rows = %d, want exactly one engine_paused row", len(audits.entries))
	}
	rec = getEngine(engine, &admin)
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `"paused":true`) {
		t.Fatalf("engine snapshot after pause = %d: %s", rec.Code, rec.Body.String())
	}

	// 方向分闸：admin 恢复 403，零写入；口令不被消费。
	before := len(audits.entries)
	if rec := putPause(`{"paused":false,"current_password":"right-pass"}`, admin.CSRFToken, &admin)(engine); rec.Code != http.StatusForbidden {
		t.Fatalf("admin resume = %d, want 403", rec.Code)
	}
	if len(audits.entries) != before {
		t.Fatalf("被拒恢复写了审计: %d -> %d", before, len(audits.entries))
	}
	if value := store.boolRows["engine.paused"]; value != "true" {
		t.Fatalf("row after rejected resume = %q, want true", value)
	}

	// master 恢复成功：200 回显 false，快照回 false；恢复也恰一行审计。
	rec = putPause(`{"paused":false,"current_password":"right-pass"}`, master.CSRFToken, &master)(engine)
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `"paused":false`) {
		t.Fatalf("master resume = %d: %s", rec.Code, rec.Body.String())
	}
	if len(audits.entries) != before+1 {
		t.Fatalf("audit rows after resume = %d, want %d", len(audits.entries), before+1)
	}
	rec = getEngine(engine, &master)
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `"paused":false`) {
		t.Fatalf("engine snapshot after resume = %d: %s", rec.Code, rec.Body.String())
	}
}

// current_password 复核的端到端形状：缺失 400、错误 403，均零写入零审计。
func TestEnginePausePasswordGateEndToEnd(t *testing.T) {
	engine, issuer, store, audits := newEnginePauseEngine(t)
	admin := issueUser(t, issuer, coreauth.RoleAdmin)

	if rec := putPause(`{"paused":true}`, admin.CSRFToken, &admin)(engine); rec.Code != http.StatusBadRequest {
		t.Fatalf("missing password = %d, want 400: %s", rec.Code, rec.Body.String())
	}
	if rec := putPause(`{"paused":true,"current_password":"wrong-pass"}`, admin.CSRFToken, &admin)(engine); rec.Code != http.StatusForbidden {
		t.Fatalf("wrong password = %d, want 403", rec.Code)
	}
	if len(store.boolRows) != 0 || len(audits.entries) != 0 {
		t.Fatalf("口令被拒路径落了行: rows=%v audits=%d", store.boolRows, len(audits.entries))
	}
}
