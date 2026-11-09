package schedule_test

// 全局暂停端点（PUT /api/admin/engine/pause，批 3a）的入口层守卫，
// 与快照 paused 字段的读面守卫。
//
// 判据：方向分闸（admin 可暂停、admin 恢复 403、user 403）、
// current_password 复核（缺 400、错 403，均零写入零审计）、
// 成功 200 回显目标态、快照带 paused（故障上抛不发快照）。
// 真路由与 CSRF 由 router 包的 e2e 钉；本文件用真用例链
// （appsettings.Service），只替存储、审计与二次口令。

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schedule"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	schedulehttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/schedule"
	"github.com/gin-gonic/gin"
)

// pauseGateFake 是暂停读写用例的替身：记录写入与读取结果。
type pauseGateFake struct {
	writes    []bool
	writesBy  []string
	auditRows []coreaudit.Entry
	paused    bool
	readErr   error
	writeErr  error
}

func (p *pauseGateFake) EnginePaused(context.Context) (bool, error) {
	if p.readErr != nil {
		return false, p.readErr
	}
	return p.paused, nil
}

func (p *pauseGateFake) SetEnginePaused(_ context.Context, scope corescope.Scope, actor appaudit.Actor, paused bool) error {
	if p.writeErr != nil {
		return p.writeErr
	}
	// 用例层的方向分闸在替身里复刻：判定入口层的门，不重复判定。
	if paused {
		if kind := scope.Kind(); kind != corescope.KindMaster && kind != corescope.KindAdmin {
			return errs.New(errs.CodeForbidden, "master or admin role is required")
		}
	} else if scope.Kind() != corescope.KindMaster {
		return errs.New(errs.CodeForbidden, "master role is required")
	}
	p.writes = append(p.writes, paused)
	p.writesBy = append(p.writesBy, actor.UserID)
	p.paused = paused
	return nil
}

// reauthFake 是二次口令替身：记录提交的口令，按值判过与拒。
type reauthFake struct {
	submitted []string
	failWith  error
}

func (r *reauthFake) Reauthenticate(_ context.Context, _ string, password string) error {
	r.submitted = append(r.submitted, password)
	return r.failWith
}

// enginePauseFixture 装配快照与暂停两个端点的处理器。
type enginePauseFixture struct {
	handler *schedulehttp.EngineHandler
	gate    *pauseGateFake
	reauth  *reauthFake
}

func newEnginePauseFixture(t *testing.T, role coreauth.Role) *enginePauseFixture {
	t.Helper()
	gate := &pauseGateFake{}
	reauth := &reauthFake{}
	handler := schedulehttp.NewEngineHandler(fakeEngineQuery{}, &fakeEngineState{}, &fakeAuthorizer{scope: adminScopeFor(role)}).
		WithPauseGate(gate, reauth)
	return &enginePauseFixture{handler: handler, gate: gate, reauth: reauth}
}

func adminScopeFor(role coreauth.Role) corescope.Scope {
	switch role {
	case coreauth.RoleMaster:
		return corescope.Master()
	case coreauth.RoleAdmin:
		return corescope.Admin(nil)
	default:
		return corescope.User("11111111-1111-1111-1111-111111111111")
	}
}

// fakeEngineQuery 与 fakeEngineState 是快照链的最小替身（快照内容不是本文件判据）。
type fakeEngineQuery struct{}

func (fakeEngineQuery) Snapshot(context.Context, corescope.Scope) (appschedule.EngineSnapshot, error) {
	return appschedule.EngineSnapshot{}, nil
}

type fakeEngineState struct{}

func (fakeEngineState) EngineTimes() (*time.Time, *time.Time, *time.Time) { return nil, nil, nil }

// pauseRequest 以指定角色与口令发一次 PUT，返回响应与解码后的错误码。
func (f *enginePauseFixture) pauseRequest(t *testing.T, body string, role coreauth.Role) *httptest.ResponseRecorder {
	t.Helper()
	gin.SetMode(gin.TestMode)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	req := httptest.NewRequest(http.MethodPut, "/api/admin/engine/pause", strings.NewReader(body))
	if role != "" {
		req = req.WithContext(coreauth.WithSession(req.Context(), coreauth.Session{
			User: coreauth.User{
				ID: "11111111-1111-1111-1111-111111111111", Email: "p@example.com",
				Role: role, Status: coreauth.StatusActive,
			},
			CSRFToken: "csrf",
		}))
	}
	c.Request = req
	f.handler.Pause(c, oapi.AdminEnginePauseParams{})
	return rec
}

// admin 暂停成功：200 回显 true，写入一次，口令被复核一次。
func TestEnginePauseAdminPauseSucceeds(t *testing.T) {
	f := newEnginePauseFixture(t, coreauth.RoleAdmin)
	rec := f.pauseRequest(t, `{"paused":true,"current_password":"pw"}`, coreauth.RoleAdmin)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d: %s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), `"paused":true`) {
		t.Fatalf("body = %s, want paused=true echo", rec.Body.String())
	}
	if len(f.gate.writes) != 1 || f.gate.writes[0] != true {
		t.Fatalf("gate writes = %v, want one true", f.gate.writes)
	}
	if len(f.reauth.submitted) != 1 || f.reauth.submitted[0] != "pw" {
		t.Fatalf("reauth submissions = %v", f.reauth.submitted)
	}
}

// 方向分闸：admin 恢复 403 且不付口令成本；user 一律 403。被拒零写入。
func TestEnginePauseDirectionGate(t *testing.T) {
	// admin 恢复：403，零写入，口令未被提交。
	f := newEnginePauseFixture(t, coreauth.RoleAdmin)
	if rec := f.pauseRequest(t, `{"paused":false,"current_password":"pw"}`, coreauth.RoleAdmin); rec.Code != http.StatusForbidden {
		t.Fatalf("admin resume = %d, want 403", rec.Code)
	}
	if len(f.gate.writes) != 0 || len(f.reauth.submitted) != 0 {
		t.Fatalf("被拒恢复触达写入或口令: writes=%v reauth=%v", f.gate.writes, f.reauth.submitted)
	}

	// user 暂停：403，零写入。
	f = newEnginePauseFixture(t, coreauth.RoleUser)
	if rec := f.pauseRequest(t, `{"paused":true,"current_password":"pw"}`, coreauth.RoleUser); rec.Code != http.StatusForbidden {
		t.Fatalf("user pause = %d, want 403", rec.Code)
	}
	if len(f.gate.writes) != 0 {
		t.Fatalf("user 暂停触达写入: %v", f.gate.writes)
	}
}

// current_password 复核：缺 400、错 403，均零写入。
func TestEnginePauseReauthGate(t *testing.T) {
	f := newEnginePauseFixture(t, coreauth.RoleMaster)
	if rec := f.pauseRequest(t, `{"paused":true}`, coreauth.RoleMaster); rec.Code != http.StatusBadRequest {
		t.Fatalf("missing password = %d, want 400", rec.Code)
	}
	if len(f.gate.writes) != 0 {
		t.Fatalf("缺口令触达写入: %v", f.gate.writes)
	}

	f = newEnginePauseFixture(t, coreauth.RoleMaster)
	f.reauth.failWith = errs.New(errs.CodeForbidden, "reauthentication failed")
	if rec := f.pauseRequest(t, `{"paused":true,"current_password":"bad"}`, coreauth.RoleMaster); rec.Code != http.StatusForbidden {
		t.Fatalf("wrong password = %d, want 403", rec.Code)
	}
	if len(f.gate.writes) != 0 {
		t.Fatalf("口令错误触达写入: %v", f.gate.writes)
	}
	if len(f.reauth.submitted) != 1 || f.reauth.submitted[0] != "bad" {
		t.Fatalf("口令复核未按提交值执行: %v", f.reauth.submitted)
	}
}

// 缺 paused 字段是 400：布尔不得被零值读成「恢复」。
func TestEnginePauseRequiresPausedField(t *testing.T) {
	f := newEnginePauseFixture(t, coreauth.RoleMaster)
	if rec := f.pauseRequest(t, `{"current_password":"pw"}`, coreauth.RoleMaster); rec.Code != http.StatusBadRequest {
		t.Fatalf("missing paused = %d, want 400", rec.Code)
	}
	if len(f.gate.writes) != 0 {
		t.Fatalf("缺字段触达写入: %v", f.gate.writes)
	}
}

// 快照带 paused：缺行=false；行 true 则 true；读故障上抛且不发快照。
func TestEngineSnapshotCarriesPaused(t *testing.T) {
	gin.SetMode(gin.TestMode)
	build := func(gate *pauseGateFake) *schedulehttp.EngineHandler {
		return schedulehttp.NewEngineHandler(fakeEngineQuery{}, &fakeEngineState{}, &fakeAuthorizer{scope: corescope.Master()}).
			WithPauseGate(gate, &reauthFake{})
	}
	snapshot := func(h *schedulehttp.EngineHandler) (string, int) {
		rec := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(rec)
		req := httptest.NewRequest(http.MethodGet, "/api/admin/engine", nil)
		req = req.WithContext(coreauth.WithSession(req.Context(), coreauth.Session{
			User: coreauth.User{ID: "11111111-1111-1111-1111-111111111111", Role: coreauth.RoleMaster, Status: coreauth.StatusActive},
		}))
		c.Request = req
		h.Snapshot(c)
		return rec.Body.String(), rec.Code
	}

	// 行 true：paused=true 出现在响应里。
	gate := &pauseGateFake{paused: true}
	body, code := snapshot(build(gate))
	if code != http.StatusOK || !strings.Contains(body, `"paused":true`) {
		t.Fatalf("snapshot = (%d, %s), want paused=true", code, body)
	}

	// 缺行（gate 返回 false）：paused=false 仍在响应里（字段总输出）。
	gate = &pauseGateFake{}
	body, code = snapshot(build(gate))
	if code != http.StatusOK || !strings.Contains(body, `"paused":false`) {
		t.Fatalf("snapshot missing-row = (%d, %s), want paused=false present", code, body)
	}

	// 读故障：快照不发，回错误信封。
	gate = &pauseGateFake{readErr: errors.New("db down")}
	body, code = snapshot(build(gate))
	if code == http.StatusOK || strings.Contains(body, `"inflight"`) {
		t.Fatalf("read failure must not return a snapshot: (%d, %s)", code, body)
	}

	// 未装配暂停面：字段仍总输出（false），其余快照不受影响。
	body, code = snapshot(schedulehttp.NewEngineHandler(fakeEngineQuery{}, &fakeEngineState{}, &fakeAuthorizer{scope: corescope.Master()}))
	if code != http.StatusOK || !strings.Contains(body, `"paused":false`) {
		t.Fatalf("unwired snapshot = (%d, %s), want paused=false present", code, body)
	}
}
