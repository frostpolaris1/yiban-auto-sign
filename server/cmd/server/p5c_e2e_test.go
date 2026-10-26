package main

// P5-C e2e：固定地点与图片注入的完整链路。
//
// # 功能
// 用真实 PostgreSQL 与假提供方走完整执行循环：
// 设固定点 → 手动签到 202 → worker Step → 断言 SignRequest
// 的 Location 与 Image；范围外按 reason 分流；缺图短路。
//
// # 归属
// cmd/server 的 e2e。复用 w2cEnv 的 schema 隔离与 HTTP 客户端。
//
// # 口径
// 模拟的真实用户动作：学生设定签到定位、上传图片、发起手动签到，
// 系统在窗口内代签。平台上传协议不在本切片（见派单稿范围修正），
// 假提供方只回成功结论，不模拟上传。

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// cityFence 是 lng 100..110、lat 20..30 的城市围栏。
func cityFence() []contract.Point {
	return []contract.Point{
		{Lng: 100, Lat: 20}, {Lng: 110, Lat: 20}, {Lng: 110, Lat: 30}, {Lng: 100, Lat: 30},
	}
}

// p5cSetup 造一个开窗、有出口、目标要求图片的环境。
// 返回环境、属主会话与账号 ID。
func p5cSetup(t *testing.T) (*w2cEnv, *coreauth.Issued, string) {
	t.Helper()
	e := w2cNewEnv(t)
	// 出口表必须有可用出口：执行循环拿不到出口会退回队列。
	if _, err := e.db.Exec(e.ctx,
		"INSERT INTO egresses (identity) VALUES ('https://egress-p5c')"); err != nil {
		t.Fatalf("seed egress: %v", err)
	}
	owner := e.user("p5c-owner@example.com", coreauth.RoleUser)
	issued := e.login(owner)
	school := e.school("p5c-loc")
	task := e.schoolTask(school.ID, "晨签")
	acct := e.account(school.ID, owner.ID, "13800040001")
	e.activateAccount(acct)
	now := clock.System{}.Now()
	day := clock.BusinessDay(now, p4Location(t))
	if _, err := e.sched.PutWindow(e.ctx, coreschedule.WindowRecord{
		SchoolTaskID: task, Day: day,
		Start: now.Add(-10 * time.Minute), End: now.Add(10 * time.Minute),
		Source: string(contract.WindowPlatform),
	}, 0); err != nil {
		t.Fatalf("put window: %v", err)
	}
	// 目标要求图片：范围外分流与缺图短路都靠它触发。
	e.provider.targets[acct] = []contract.Target{{
		ID:            "pos-1",
		Title:         "东门",
		Polygon:       cityFence(),
		RequiresPhoto: true,
	}}
	return e, issued, acct
}

// setLocation 发一次定位策略写入并断言状态码。
func p5cSetLocation(t *testing.T, e *w2cEnv, issued *coreauth.Issued, acct string, body map[string]any) int {
	t.Helper()
	rec := e.do("PUT", "/api/accounts/"+acct+"/location", body, issued)
	return rec.Code
}

// p5cSign 发起手动签到并断言 202。
func p5cSign(t *testing.T, e *w2cEnv, issued *coreauth.Issued, acct string) {
	t.Helper()
	rec := e.do("POST", "/api/accounts/"+acct+"/sign", nil, issued)
	if rec.Code != 202 {
		t.Fatalf("sign status = %d: %s", rec.Code, rec.Body.String())
	}
}

// p5cResult 读账号当日任务的 (state, result)。
func p5cResult(e *w2cEnv, acct string) (string, string) {
	e.t.Helper()
	state, err := e.db.QueryString(e.ctx,
		"SELECT state::text FROM sign_tasks WHERE account_id = $1::uuid ORDER BY created_at DESC LIMIT 1",
		acct)
	if err != nil {
		e.t.Fatalf("query state: %v", err)
	}
	result, err := e.db.QueryString(e.ctx,
		"SELECT result FROM sign_tasks WHERE account_id = $1::uuid ORDER BY created_at DESC LIMIT 1",
		acct)
	if err != nil {
		e.t.Fatalf("query result: %v", err)
	}
	return state, result
}

// 链路一：围栏内固定点＋有图 → SignRequest 带定位与图片，结论 success。
func TestP5CInjectsLocationAndImage(t *testing.T) {
	e, issued, acct := p5cSetup(t)
	if code := p5cSetLocation(t, e, issued, acct, map[string]any{
		"mode": "fixed", "lng": 105.0, "lat": 25.0, "reason": "",
	}); code != 200 {
		t.Fatalf("set location status = %d", code)
	}
	// 上传一张图：签到链从这里选图。
	rec := e.uploadPhoto(issued, pngPhoto(t, 8, 8))
	if rec.Code != 201 {
		t.Fatalf("upload status = %d: %s", rec.Code, rec.Body.String())
	}
	var photo oapi.Photo
	if err := json.Unmarshal(rec.Body.Bytes(), &photo); err != nil {
		t.Fatalf("decode photo: %v", err)
	}

	p5cSign(t, e, issued, acct)
	n, err := e.worker.Step(e.ctx, 1)
	if err != nil || n != 1 {
		t.Fatalf("Step = (%d,%v)", n, err)
	}
	if len(e.provider.signs) != 1 {
		t.Fatalf("签到提交 %d 次, want 1", len(e.provider.signs))
	}
	req := e.provider.signs[0]
	if req.Location.Mode != contract.LocationFixed ||
		req.Location.Lng != 105 || req.Location.Lat != 25 {
		t.Fatalf("Location = %+v", req.Location)
	}
	if req.Image == nil {
		t.Fatal("要求图片的目标收到了 nil Image")
	}
	if req.Image.SHA256 != photo.Sha256 || req.Image.Mime != string(photo.Mime) {
		t.Fatalf("Image = %+v, want sha %s", req.Image, photo.Sha256)
	}
	if len(req.Image.Bytes) == 0 {
		t.Fatal("Image 字节为空")
	}
	if state, result := p5cResult(e, acct); state != string(corestatus.TaskSuccess) || result != string(contract.OutcomeSuccess) {
		t.Fatalf("终态 = %s/%s", state, result)
	}
}

// 链路二：围栏外无原因 → skipped/location_out_of_range，不提交签到。
func TestP5COutOfRangeWithoutReasonSkips(t *testing.T) {
	e, issued, acct := p5cSetup(t)
	if code := p5cSetLocation(t, e, issued, acct, map[string]any{
		"mode": "fixed", "lng": 0.0, "lat": 0.0,
	}); code != 200 {
		t.Fatalf("set location status = %d", code)
	}
	// 图片已上传：越界是唯一短路原因。
	if rec := e.uploadPhoto(issued, pngPhoto(t, 8, 8)); rec.Code != 201 {
		t.Fatalf("upload status = %d", rec.Code)
	}

	p5cSign(t, e, issued, acct)
	if _, err := e.worker.Step(e.ctx, 1); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if len(e.provider.signs) != 0 {
		t.Fatalf("越界无原因仍提交了 %d 次签到", len(e.provider.signs))
	}
	state, result := p5cResult(e, acct)
	if state != string(corestatus.TaskSkipped) || result != "location_out_of_range" {
		t.Fatalf("终态 = %s/%s, want skipped/location_out_of_range", state, result)
	}
}

// 链路三：围栏外但有原因 → 放行，原因进 SignRequest。
func TestP5COutOfRangeWithReasonProceeds(t *testing.T) {
	e, issued, acct := p5cSetup(t)
	if code := p5cSetLocation(t, e, issued, acct, map[string]any{
		"mode": "fixed", "lng": 0.0, "lat": 0.0, "reason": "在校门口，定位漂移",
	}); code != 200 {
		t.Fatalf("set location status = %d", code)
	}
	if rec := e.uploadPhoto(issued, pngPhoto(t, 8, 8)); rec.Code != 201 {
		t.Fatalf("upload status = %d", rec.Code)
	}

	p5cSign(t, e, issued, acct)
	if _, err := e.worker.Step(e.ctx, 1); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if len(e.provider.signs) != 1 {
		t.Fatalf("带原因应放行, signs = %d", len(e.provider.signs))
	}
	req := e.provider.signs[0]
	if req.Location.Reason != "在校门口，定位漂移" {
		t.Fatalf("Reason = %q", req.Location.Reason)
	}
	if req.Image == nil {
		t.Fatal("放行的签到仍需带图")
	}
}

// 链路四：库空且目标要求图 → skipped/photo_missing，不提交签到。
func TestP5CPhotoMissingSkips(t *testing.T) {
	e, issued, acct := p5cSetup(t)
	// 不设定位（auto 不判围栏）、不上传图片。
	p5cSign(t, e, issued, acct)
	if _, err := e.worker.Step(e.ctx, 1); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if len(e.provider.signs) != 0 {
		t.Fatalf("缺图仍提交了 %d 次签到", len(e.provider.signs))
	}
	state, result := p5cResult(e, acct)
	if state != string(corestatus.TaskSkipped) || result != string(contract.OutcomePhotoMissing) {
		t.Fatalf("终态 = %s/%s, want skipped/photo_missing", state, result)
	}
}

// 定位端点的入口判据：跨用户 404、跨校 admin 404、非法 mode 400。
func TestP5CLocationEndpointGuards(t *testing.T) {
	e, issued, acct := p5cSetup(t)
	other := e.user("p5c-other@example.com", coreauth.RoleUser)
	otherIssued := e.login(other)
	admin := e.user("p5c-admin@example.com", coreauth.RoleAdmin)
	adminIssued := e.login(admin)

	// user 写他人账号：404。
	rec := e.do("PUT", "/api/accounts/"+acct+"/location", map[string]any{"mode": "auto"}, otherIssued)
	if rec.Code != 404 {
		t.Fatalf("他人写入 status = %d: %s", rec.Code, rec.Body.String())
	}
	// 未绑定校 admin：404。
	e.bind(admin.ID, e.school("p5c-other-school").ID)
	rec = e.do("PUT", "/api/accounts/"+acct+"/location", map[string]any{"mode": "auto"}, adminIssued)
	if rec.Code != 404 {
		t.Fatalf("跨校 admin status = %d", rec.Code)
	}
	// 本人写入成功；响应回显完整策略。
	rec = e.do("PUT", "/api/accounts/"+acct+"/location", map[string]any{
		"mode": "fixed", "lng": 105.0, "lat": 25.0, "reason": "宿舍",
	}, issued)
	if rec.Code != 200 {
		t.Fatalf("本人写入 status = %d: %s", rec.Code, rec.Body.String())
	}
	if body := rec.Body.String(); !strings.Contains(body, `"mode":"fixed"`) || !strings.Contains(body, `"reason":"宿舍"`) {
		t.Fatalf("响应体 = %s", body)
	}
	// 非法 mode：400。
	rec = e.do("PUT", "/api/accounts/"+acct+"/location", map[string]any{"mode": "gps"}, issued)
	if rec.Code != 400 {
		t.Fatalf("非法 mode status = %d", rec.Code)
	}
	// 越界坐标：400。
	rec = e.do("PUT", "/api/accounts/"+acct+"/location", map[string]any{
		"mode": "fixed", "lng": 200.0, "lat": 0.0,
	}, issued)
	if rec.Code != 400 {
		t.Fatalf("越界坐标 status = %d", rec.Code)
	}
}
