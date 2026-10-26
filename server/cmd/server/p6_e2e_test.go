package main

// P6 e2e：管理员 SSE、诊断与用户轮询的完整链路（真库）。
//
// # 功能
// 手动签到 → Step 结算 → SSE 流读到 task.concluded；
// sign-status 的 stale 翻转；诊断端点的形状、跨面 404 与词表；
// 全响应的隐私扫描（无手机号形状、无凭据字段名）。
//
// # 口径
// 模拟的真实用户动作：学生发起签到，管理员在事件页看到结算，
// 学生页轮询签到状态。SSE 用真 HTTP 服务器流式读取。

import (
	"bufio"
	"context"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	appevents "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/events"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// p6Stream 是一条 SSE 连接的读取端。
type p6Stream struct {
	frames chan p6Frame
	cancel context.CancelFunc
}

type p6Frame struct {
	id    string
	event string
	data  string
}

// p6OpenStream 打开 SSE 流并后台读帧。
func p6OpenStream(t *testing.T, e *w2cEnv, issued *coreauth.Issued, lastEventID string) *p6Stream {
	t.Helper()
	srv := httptest.NewServer(e.engine)
	t.Cleanup(srv.Close)
	ctx, cancel := context.WithCancel(context.Background())
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, srv.URL+"/api/admin/events", nil)
	if err != nil {
		cancel()
		t.Fatalf("new request: %v", err)
	}
	req.AddCookie(&http.Cookie{Name: coreauth.InsecureCookieName, Value: issued.Token})
	if lastEventID != "" {
		req.Header.Set("Last-Event-ID", lastEventID)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		cancel()
		t.Fatalf("open stream: %v", err)
	}
	if resp.StatusCode != http.StatusOK {
		resp.Body.Close()
		cancel()
		t.Fatalf("stream status = %d", resp.StatusCode)
	}
	st := &p6Stream{frames: make(chan p6Frame, 64), cancel: cancel}
	go func() {
		defer close(st.frames)
		scanner := bufio.NewScanner(resp.Body)
		var cur p6Frame
		for scanner.Scan() {
			line := scanner.Text()
			switch {
			case strings.HasPrefix(line, "id: "):
				cur.id = strings.TrimPrefix(line, "id: ")
			case strings.HasPrefix(line, "event: "):
				cur.event = strings.TrimPrefix(line, "event: ")
			case strings.HasPrefix(line, "data: "):
				cur.data = strings.TrimPrefix(line, "data: ")
			case line == "":
				if cur.event != "" {
					st.frames <- cur
				}
				cur = p6Frame{}
			}
		}
	}()
	t.Cleanup(func() {
		cancel()
		resp.Body.Close()
	})
	return st
}

func (s *p6Stream) next(t *testing.T) p6Frame {
	t.Helper()
	select {
	case f, ok := <-s.frames:
		if !ok {
			t.Fatal("流已关闭，未等到帧")
		}
		return f
	case <-time.After(3 * time.Second):
		t.Fatal("等帧超时")
		return p6Frame{}
	}
}

// p6Setup 造开窗、有出口的单账号环境，返回环境、属主会话与账号 ID。
func p6Setup(t *testing.T) (*w2cEnv, *coreauth.Issued, string) {
	t.Helper()
	e := w2cNewEnv(t)
	if _, err := e.db.Exec(e.ctx,
		"INSERT INTO egresses (identity) VALUES ('https://egress-p6')"); err != nil {
		t.Fatalf("seed egress: %v", err)
	}
	owner := e.user("p6-owner@example.com", coreauth.RoleUser)
	issued := e.login(owner)
	school := e.school("p6-school")
	task := e.schoolTask(school.ID, "晨签")
	acct := e.account(school.ID, owner.ID, "13800060001")
	e.activateAccount(acct)
	// 预置平台目标：缺省时 DiscoverTargets 报 upstream，任务走退避，
	// 永远到不了结算（P5-C 同款设置，不要求图片）。
	e.provider.targets[acct] = []contract.Target{{ID: "pos-1", Title: "东门"}}
	now := clock.System{}.Now()
	day := clock.BusinessDay(now, p4Location(t))
	if _, err := e.sched.PutWindow(e.ctx, coreschedule.WindowRecord{
		SchoolTaskID: task, Day: day,
		Start: now.Add(-10 * time.Minute), End: now.Add(10 * time.Minute),
		Source: string(contract.WindowPlatform),
	}, 0); err != nil {
		t.Fatalf("put window: %v", err)
	}
	return e, issued, acct
}

// 链路一：手动签到 → Step 结算 → master 的 SSE 流读到 task.concluded。
func TestP6SSEConcludedTask(t *testing.T) {
	e, issued, acct := p6Setup(t)
	master := e.user("p6-master@example.com", coreauth.RoleMaster)
	masterIssued := e.login(master)

	stream := p6OpenStream(t, e, masterIssued, "")
	p5cSign(t, e, issued, acct)
	if _, err := e.worker.Step(e.ctx, 1); err != nil {
		t.Fatalf("Step: %v", err)
	}

	f := stream.next(t)
	if f.event != appevents.TypeTaskConcluded {
		t.Fatalf("首帧 = %q, want task.concluded", f.event)
	}
	if f.id == "" {
		t.Fatal("数据帧缺 id")
	}
	var data struct {
		SchoolID string         `json:"school_id"`
		TaskID   string         `json:"task_id"`
		Day      string         `json:"day"`
		At       string         `json:"at"`
		Payload  map[string]any `json:"payload"`
	}
	if err := json.Unmarshal([]byte(f.data), &data); err != nil {
		t.Fatalf("data 非法: %q", f.data)
	}
	if data.Payload["account_id"] != acct {
		t.Fatalf("payload = %v, want account %s", data.Payload, acct)
	}
	if data.Payload["state"] != string(corestatus.TaskSuccess) {
		t.Fatalf("payload state = %v", data.Payload["state"])
	}
	if data.At == "" || data.Day == "" {
		t.Fatalf("信封缺 at/day: %+v", data)
	}
}

// 链路二：admin 只收绑定校事件；跨校事件不出现在流里。
func TestP6SSEAdminScopeFilter(t *testing.T) {
	e, _, _ := p6Setup(t)
	admin := e.user("p6-admin@example.com", coreauth.RoleAdmin)
	adminIssued := e.login(admin)
	boundSchool := e.school("p6-bound")
	e.bind(admin.ID, boundSchool.ID)

	stream := p6OpenStream(t, e, adminIssued, "")
	// 校 A（未绑定）真实结算；绑定校注入一条事件。
	e.broker.Publish([]string{"s-unbound"}, appevents.Event{
		Type: appevents.TypeTaskProgress, SchoolID: "s-unbound",
		Payload: map[string]any{"account_id": "x"},
	})
	e.broker.Publish([]string{boundSchool.ID}, appevents.Event{
		Type: appevents.TypeTaskProgress, SchoolID: boundSchool.ID,
		Payload: map[string]any{"account_id": "y"},
	})
	f := stream.next(t)
	if f.event != appevents.TypeTaskProgress {
		t.Fatalf("首帧 = %q", f.event)
	}
	var data struct {
		SchoolID string `json:"school_id"`
	}
	if err := json.Unmarshal([]byte(f.data), &data); err != nil {
		t.Fatalf("data 非法: %q", f.data)
	}
	if data.SchoolID != boundSchool.ID {
		t.Fatalf("admin 收到非绑定校事件: %v", data)
	}
}

// 链路三：Last-Event-ID 断线续传——重连后补发缓冲内事件。
func TestP6SSEResume(t *testing.T) {
	e, _, _ := p6Setup(t)
	master := e.user("p6-master2@example.com", coreauth.RoleMaster)
	masterIssued := e.login(master)

	stream := p6OpenStream(t, e, masterIssued, "")
	e.broker.Publish([]string{"s1"}, appevents.Event{Type: appevents.TypeTaskProgress, SchoolID: "s1"})
	first := stream.next(t)
	stream.cancel()

	reconnected := p6OpenStream(t, e, masterIssued, first.id)
	e.broker.Publish([]string{"s1"}, appevents.Event{Type: appevents.TypeTaskProgress, SchoolID: "s1"})
	next := reconnected.next(t)
	if next.event != appevents.TypeTaskProgress {
		t.Fatalf("续传首帧 = %q", next.event)
	}
}

// 链路四：sign-status 返回 items；updated_at 超 90s 翻转 stale。
func TestP6SignStatusStaleFlip(t *testing.T) {
	e, issued, acct := p6Setup(t)
	p5cSign(t, e, issued, acct) // 建当日任务行（pending）

	rec := e.do("GET", "/api/me/sign-status", nil, issued)
	if rec.Code != 200 {
		t.Fatalf("sign-status status = %d: %s", rec.Code, rec.Body.String())
	}
	var body struct {
		UpdatedAt string `json:"updated_at"`
		Items     []struct {
			AccountID string  `json:"account_id"`
			State     *string `json:"state"`
			RunAt     *string `json:"run_at"`
			Stale     bool    `json:"stale"`
		} `json:"items"`
	}
	e.decode(rec, &body)
	if len(body.Items) != 1 || body.Items[0].AccountID != acct {
		t.Fatalf("items = %+v", body.Items)
	}
	if body.Items[0].Stale {
		t.Fatal("刚建行不应 stale")
	}
	if body.Items[0].State == nil || *body.Items[0].State != string(corestatus.TaskPending) {
		t.Fatalf("state = %v", body.Items[0].State)
	}

	// 把 updated_at 拨回 91s（触发器会刷新，先禁用再恢复）。
	if _, err := e.db.Exec(e.ctx,
		"ALTER TABLE sign_tasks DISABLE TRIGGER trg_sign_tasks_updated_at"); err != nil {
		t.Fatalf("disable trigger: %v", err)
	}
	if _, err := e.db.Exec(e.ctx,
		`UPDATE sign_tasks SET updated_at = updated_at - interval '95 seconds' WHERE account_id = $1::uuid`,
		acct); err != nil {
		t.Fatalf("backdate: %v", err)
	}
	if _, err := e.db.Exec(e.ctx,
		"ALTER TABLE sign_tasks ENABLE TRIGGER trg_sign_tasks_updated_at"); err != nil {
		t.Fatalf("enable trigger: %v", err)
	}
	rec = e.do("GET", "/api/me/sign-status", nil, issued)
	e.decode(rec, &body)
	if !body.Items[0].Stale {
		t.Fatal("95s 未更新应 stale")
	}
}

// 链路五：诊断端点形状、跨用户 404、suggestion 词表封闭。
func TestP6DiagnosticsEndpoints(t *testing.T) {
	e, issued, acct := p6Setup(t)
	p5cSign(t, e, issued, acct)

	rec := e.do("GET", "/api/me/diagnostics", nil, issued)
	if rec.Code != 200 {
		t.Fatalf("diagnostics status = %d: %s", rec.Code, rec.Body.String())
	}
	var body struct {
		Items []struct {
			AccountID  string  `json:"account_id"`
			State      *string `json:"state"`
			Suggestion string  `json:"suggestion"`
		} `json:"items"`
	}
	e.decode(rec, &body)
	if len(body.Items) != 1 || body.Items[0].AccountID != acct {
		t.Fatalf("items = %+v", body.Items)
	}
	suggestion := body.Items[0].Suggestion
	switch suggestion {
	case "wait", "retry_now", "re_arm", "contact_admin":
	default:
		t.Fatalf("suggestion %q 不在词表内", suggestion)
	}

	// 单账号详版。
	rec = e.do("GET", "/api/accounts/"+acct+"/diagnostics", nil, issued)
	if rec.Code != 200 {
		t.Fatalf("detail status = %d: %s", rec.Code, rec.Body.String())
	}
	// 跨用户 404。
	other := e.user("p6-other@example.com", coreauth.RoleUser)
	otherIssued := e.login(other)
	if rec := e.do("GET", "/api/accounts/"+acct+"/diagnostics", nil, otherIssued); rec.Code != 404 {
		t.Fatalf("跨用户 status = %d, want 404", rec.Code)
	}
	// 跨校 admin 404。
	admin := e.user("p6-admin2@example.com", coreauth.RoleAdmin)
	adminIssued := e.login(admin)
	e.bind(admin.ID, e.school("p6-other-school").ID)
	if rec := e.do("GET", "/api/accounts/"+acct+"/diagnostics", nil, adminIssued); rec.Code != 404 {
		t.Fatalf("跨校 admin status = %d, want 404", rec.Code)
	}
	// user 无事件流端点：403。
	if rec := e.do("GET", "/api/admin/events", nil, issued); rec.Code != 403 {
		t.Fatalf("user 订阅 status = %d, want 403", rec.Code)
	}
}

// 链路六：隐私扫描——本文件所有响应体与 SSE 数据帧
// 不含手机号形状与凭据字段名。
func TestP6PrivacyScan(t *testing.T) {
	e, issued, acct := p6Setup(t)
	masterIssued := e.login(e.user("p6-master3@example.com", coreauth.RoleMaster))
	stream := p6OpenStream(t, e, masterIssued, "")
	p5cSign(t, e, issued, acct)
	if _, err := e.worker.Step(e.ctx, 1); err != nil {
		t.Fatalf("Step: %v", err)
	}
	f := stream.next(t)
	for _, path := range []string{
		"/api/me/diagnostics", "/api/me/sign-status",
		"/api/accounts/" + acct + "/diagnostics",
	} {
		if rec := e.do("GET", path, nil, issued); rec.Code != 200 {
			t.Fatalf("%s status = %d", path, rec.Code)
		}
	}
	bodies := append([]string{f.data}, e.bodies...)
	for i, b := range bodies {
		if w2cPhoneShape.MatchString(b) {
			t.Fatalf("响应 %d 含手机号形状: %s", i, b)
		}
		for _, field := range []string{"password", "device_code", "credential", "secret"} {
			if strings.Contains(strings.ToLower(b), field) {
				t.Fatalf("响应 %d 含凭据字段名 %q: %s", i, field, b)
			}
		}
	}
}

// P6 审查 P1 回归：全局 http.Server 的 WriteTimeout 不得掐断 SSE 长流。
// 处理器必须用 NewResponseController 清掉本次响应的写超时；
// 否则 50ms（测试值）后任何写入失败、流断，15s 心跳根本活不到第二跳。
func TestP6SSESurvivesServerWriteTimeout(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("p6-wt-master@example.com", coreauth.RoleMaster)
	issued := e.login(master)
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	srv := &http.Server{Handler: e.engine, WriteTimeout: 50 * time.Millisecond}
	go func() { _ = srv.Serve(ln) }()
	t.Cleanup(func() { _ = srv.Close() })

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet,
		"http://"+ln.Addr().String()+"/api/admin/events", nil)
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	req.AddCookie(&http.Cookie{Name: coreauth.InsecureCookieName, Value: issued.Token})
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("open stream: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("stream status = %d", resp.StatusCode)
	}

	// 200ms（> WriteTimeout）后发事件：清了写超时才收得到。
	time.AfterFunc(200*time.Millisecond, func() {
		e.broker.Publish([]string{"s-unbound"}, appevents.Event{
			Type: appevents.TypeTaskProgress, SchoolID: "s-unbound",
		})
	})

	frames := make(chan p6Frame, 8)
	go func() {
		defer close(frames)
		scanner := bufio.NewScanner(resp.Body)
		var cur p6Frame
		for scanner.Scan() {
			line := scanner.Text()
			switch {
			case strings.HasPrefix(line, "event: "):
				cur.event = strings.TrimPrefix(line, "event: ")
			case line == "":
				if cur.event != "" {
					frames <- cur
				}
				cur = p6Frame{}
			}
		}
	}()
	for {
		select {
		case f, ok := <-frames:
			if !ok {
				t.Fatal("流在收到事件前被服务端关闭（写超时未清）")
			}
			if f.event == string(appevents.TypeTaskProgress) {
				return
			}
		case <-time.After(3 * time.Second):
			t.Fatal("3 秒内未收到写超时后发布的事件")
		}
	}
}
