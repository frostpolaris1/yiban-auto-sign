package events_test

// SSE 端点判据（P6）：帧格式（id/event/data）、15s 心跳（假时钟）、
// 60s 空闲 end 并关闭、Last-Event-ID 续传、缺口 resync+snapshot、
// admin 只收绑定校、user 403、请求取消即断流、no-store 响应头。
// 流式读取用真 HTTP 服务器：httptest.ResponseRecorder 不支持流。

import (
	"bufio"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	appevents "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/events"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	eventshttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/events"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/router"
)

// fakeClock 是可手动推进的时钟。
type fakeClock struct {
	mu sync.Mutex
	t  time.Time
}

func newFakeClock(t time.Time) *fakeClock { return &fakeClock{t: t} }

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *fakeClock) Advance(d time.Duration) {
	c.mu.Lock()
	c.t = c.t.Add(d)
	c.mu.Unlock()
}

// fakeTimer 记住处理器最近要的定时通道；Fire 模拟到期。
// Fire 发的是假时钟的当前时刻：定时到点与处理器读到的 now 同源。
type fakeTimer struct {
	mu sync.Mutex
	ch chan time.Time
	c  *fakeClock
}

func newFakeTimer() *fakeTimer { return &fakeTimer{} }

func (f *fakeTimer) After(time.Duration) <-chan time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.ch = make(chan time.Time, 1)
	return f.ch
}

func (f *fakeTimer) fireAt(t time.Time) {
	f.mu.Lock()
	ch := f.ch
	f.mu.Unlock()
	if ch != nil {
		ch <- t
	}
}

// frame 是解析后的一条 SSE 帧。
type frame struct {
	id    string
	event string
	data  string
}

// streamClient 是一条真 HTTP SSE 连接的读取端。
type streamClient struct {
	resp   *http.Response
	frames chan frame
	errs   chan error
	cancel context.CancelFunc
}

func newStreamClient(t *testing.T, base string, issued coreauth.Issued, lastEventID string) *streamClient {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, base+"/api/admin/events", nil)
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
		t.Fatalf("do: %v", err)
	}
	if resp.StatusCode != http.StatusOK {
		resp.Body.Close()
		cancel()
		t.Fatalf("stream status = %d", resp.StatusCode)
	}
	sc := &streamClient{
		resp:   resp,
		frames: make(chan frame, 32),
		errs:   make(chan error, 1),
		cancel: cancel,
	}
	go sc.readLoop()
	return sc
}

func (sc *streamClient) readLoop() {
	defer close(sc.frames)
	scanner := bufio.NewScanner(sc.resp.Body)
	var cur frame
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
			if cur.event != "" || cur.data != "" {
				sc.frames <- cur
			}
			cur = frame{}
		}
	}
	if err := scanner.Err(); err != nil {
		sc.errs <- err
	}
}

// next 读下一帧或超时失败。静默等待不是绿。
func (sc *streamClient) next(t *testing.T) frame {
	t.Helper()
	select {
	case f, ok := <-sc.frames:
		if !ok {
			t.Fatal("流已关闭，未等到帧")
		}
		return f
	case err := <-sc.errs:
		t.Fatalf("读流出错: %v", err)
		return frame{}
	case <-time.After(5 * time.Second):
		t.Fatal("等帧超时")
		return frame{}
	}
}

// closed 报告流是否已结束（读到 EOF）。
func (sc *streamClient) closed(t *testing.T) bool {
	t.Helper()
	select {
	case _, ok := <-sc.frames:
		return !ok
	case <-time.After(5 * time.Second):
		return false
	}
}

func (sc *streamClient) close() {
	sc.cancel()
	sc.resp.Body.Close()
}

// newSSEServer 构造装配 SSE 域的真 HTTP 服务器。
func newSSEServer(t *testing.T, authz *fakeAuthorizer, diag *fakeDiag, broker *appevents.Broker, clk *fakeClock, timer *fakeTimer) (*httptest.Server, coreauth.Issued) {
	t.Helper()
	issuer := &fakeIssuer{secret: []byte("0123456789abcdef0123456789abcdef")}
	issued, err := issuer.Issue(context.Background(), coreauth.User{
		ID: "u1", Email: "master@example.com", Role: coreauth.RoleMaster, Status: coreauth.StatusActive,
	})
	if err != nil {
		t.Fatalf("issue session: %v", err)
	}
	if authz == nil {
		authz = &fakeAuthorizer{scope: corescope.Master()}
	}
	handler := eventshttp.NewHandler(broker, diag, authz, clk, timer)
	engine, err := router.New(router.Options{
		Sessions: issuer, InsecureCookies: true, Events: handler,
	})
	if err != nil {
		t.Fatalf("router: %v", err)
	}
	srv := httptest.NewServer(engine)
	t.Cleanup(srv.Close)
	return srv, issued
}

func progressEvent(school string) appevents.Event {
	return appevents.Event{
		Type: appevents.TypeTaskProgress, SchoolID: school,
		TaskID: "st-1", Day: "2026-10-29",
		At:      diagNowTime,
		Payload: map[string]any{"account_id": "a1", "attempt": 1},
	}
}

// 帧格式：id/event/data 三行成对出现，data 是契约 JSON。
func TestSSEFrameFormat(t *testing.T) {
	broker := appevents.NewBroker()
	diag := &fakeDiag{counts: map[string]int{}}
	srv, issued := newSSEServer(t, nil, diag, broker, newFakeClock(diagNowTime), newFakeTimer())
	sc := newStreamClient(t, srv.URL, issued, "")
	defer sc.close()

	broker.Publish([]string{"s1"}, progressEvent("s1"))
	f := sc.next(t)
	if f.id != "1" {
		t.Fatalf("id = %q, want 1", f.id)
	}
	if f.event != appevents.TypeTaskProgress {
		t.Fatalf("event = %q", f.event)
	}
	var data map[string]any
	if err := json.Unmarshal([]byte(f.data), &data); err != nil {
		t.Fatalf("data 非合法 JSON: %q", f.data)
	}
	if data["school_id"] != "s1" || data["task_id"] != "st-1" || data["day"] != "2026-10-29" {
		t.Fatalf("data = %v", data)
	}
	payload, ok := data["payload"].(map[string]any)
	if !ok || payload["account_id"] != "a1" {
		t.Fatalf("payload = %v", data["payload"])
	}
	if _, ok := data["at"]; !ok {
		t.Fatal("at 缺失")
	}
}

// 响应头：text/event-stream、no-store、X-Accel-Buffering: no。
func TestSSEHeaders(t *testing.T) {
	broker := appevents.NewBroker()
	srv, issued := newSSEServer(t, nil, &fakeDiag{}, broker, newFakeClock(diagNowTime), newFakeTimer())
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, srv.URL+"/api/admin/events", nil)
	req.AddCookie(&http.Cookie{Name: coreauth.InsecureCookieName, Value: issued.Token})
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("do: %v", err)
	}
	defer resp.Body.Close()
	if ct := resp.Header.Get("Content-Type"); !strings.HasPrefix(ct, "text/event-stream") {
		t.Fatalf("content-type = %q", ct)
	}
	if cc := resp.Header.Get("Cache-Control"); cc != "no-store" {
		t.Fatalf("cache-control = %q", cc)
	}
	if ab := resp.Header.Get("X-Accel-Buffering"); ab != "no" {
		t.Fatalf("x-accel-buffering = %q", ab)
	}
}

// 心跳与空闲：15s 心跳帧；60s 无事件 → end 帧并关闭。
func TestSSEHeartbeatAndIdleEnd(t *testing.T) {
	clk := newFakeClock(diagNowTime)
	timer := newFakeTimer()
	broker := appevents.NewBroker()
	srv, issued := newSSEServer(t, nil, &fakeDiag{}, broker, clk, timer)
	sc := newStreamClient(t, srv.URL, issued, "")
	defer sc.close()

	clk.Advance(15 * time.Second)
	timer.fireAt(clk.Now())
	f := sc.next(t)
	if f.event != appevents.TypeHeartbeat {
		t.Fatalf("15s 帧 = %q, want heartbeat", f.event)
	}
	if f.id != "" {
		t.Fatalf("心跳帧不应带 id，got %q", f.id)
	}
	clk.Advance(45 * time.Second) // 距上一事件 60s
	timer.fireAt(clk.Now())
	f = sc.next(t)
	if f.event != appevents.TypeEnd {
		t.Fatalf("60s 帧 = %q, want end", f.event)
	}
	if !sc.closed(t) {
		t.Fatal("end 帧后流未关闭")
	}
}

// 心跳不算事件：中间的空闲判据不因心跳重置。
func TestSSEHeartbeatResetsOnlyItsOwnCadence(t *testing.T) {
	clk := newFakeClock(diagNowTime)
	timer := newFakeTimer()
	broker := appevents.NewBroker()
	srv, issued := newSSEServer(t, nil, &fakeDiag{}, broker, clk, timer)
	sc := newStreamClient(t, srv.URL, issued, "")
	defer sc.close()

	clk.Advance(15 * time.Second)
	timer.fireAt(clk.Now())
	if f := sc.next(t); f.event != appevents.TypeHeartbeat {
		t.Fatalf("第一帧 = %q", f.event)
	}
	broker.Publish([]string{"s1"}, progressEvent("s1"))
	if f := sc.next(t); f.event != appevents.TypeTaskProgress {
		t.Fatalf("事件帧 = %q", f.event)
	}
	// 事件把空闲计时重置：再过 45s 只该有 3 次心跳，无 end。
	for i := 0; i < 3; i++ {
		clk.Advance(15 * time.Second)
		timer.fireAt(clk.Now())
		if f := sc.next(t); f.event != appevents.TypeHeartbeat {
			t.Fatalf("第 %d 次 15s 应为心跳，got %q", i+1, f.event)
		}
	}
}

// 续传：Last-Event-ID 之后缓冲内事件按序补发。
func TestSSEResumeWithLastEventID(t *testing.T) {
	broker := appevents.NewBroker()
	broker.Publish([]string{"s1"}, progressEvent("s1"))
	broker.Publish([]string{"s1"}, progressEvent("s1"))
	srv, issued := newSSEServer(t, nil, &fakeDiag{}, broker, newFakeClock(diagNowTime), newFakeTimer())
	sc := newStreamClient(t, srv.URL, issued, "1")
	defer sc.close()

	f := sc.next(t)
	if f.id != "2" || f.event != appevents.TypeTaskProgress {
		t.Fatalf("续传首帧 = %q/%q, want 2/task.progress", f.id, f.event)
	}
}

// 缺口：落后超过缓冲 → resync 帧 + snapshot 帧，旧事件不补发。
func TestSSEResyncThenSnapshot(t *testing.T) {
	broker := appevents.NewBroker()
	// RingCapacity+2 条使环满且最旧 ID=3：lastEventID=1 落在环外 → 缺口。
	for i := 0; i < appevents.RingCapacity+2; i++ {
		broker.Publish([]string{"s1"}, progressEvent("s1"))
	}
	diag := &fakeDiag{counts: map[string]int{string(status.TaskPending): 3, string(status.TaskSuccess): 7}}
	srv, issued := newSSEServer(t, nil, diag, broker, newFakeClock(diagNowTime), newFakeTimer())
	sc := newStreamClient(t, srv.URL, issued, "1")
	defer sc.close()

	resync := sc.next(t)
	if resync.event != appevents.TypeResync {
		t.Fatalf("首帧 = %q, want resync", resync.event)
	}
	if resync.id != "" {
		t.Fatalf("resync 帧不应带 id，got %q", resync.id)
	}
	snap := sc.next(t)
	if snap.event != appevents.TypeSnapshot {
		t.Fatalf("第二帧 = %q, want snapshot", snap.event)
	}
	var data map[string]any
	if err := json.Unmarshal([]byte(snap.data), &data); err != nil {
		t.Fatalf("snapshot data 非法: %q", snap.data)
	}
	states, ok := data["states"].(map[string]any)
	if !ok || states[string(status.TaskPending)] != float64(3) || states[string(status.TaskSuccess)] != float64(7) {
		t.Fatalf("states = %v", data["states"])
	}
	if _, ok := data["day"]; !ok {
		t.Fatal("snapshot 缺 day")
	}
	// snapshot 后 live 正常。
	broker.Publish([]string{"s1"}, progressEvent("s1"))
	f := sc.next(t)
	if f.event != appevents.TypeTaskProgress {
		t.Fatalf("resync 后 live 帧 = %q", f.event)
	}
}

// admin 只收绑定校：跨校事件不出现在流里。
func TestSSEAdminScopeFilter(t *testing.T) {
	broker := appevents.NewBroker()
	adminScope := corescope.Admin([]string{"s1"})
	srv, issued := newSSEServer(t, &fakeAuthorizer{scope: adminScope}, &fakeDiag{}, broker, newFakeClock(diagNowTime), newFakeTimer())
	sc := newStreamClient(t, srv.URL, issued, "")
	defer sc.close()

	broker.Publish([]string{"other"}, progressEvent("other"))
	broker.Publish([]string{"s1"}, progressEvent("s1"))
	f := sc.next(t)
	if f.event != appevents.TypeTaskProgress {
		t.Fatalf("首帧 = %q", f.event)
	}
	var data map[string]any
	if err := json.Unmarshal([]byte(f.data), &data); err != nil {
		t.Fatalf("data 非法: %q", f.data)
	}
	if data["school_id"] != "s1" {
		t.Fatalf("admin 收到跨校事件: %v", data)
	}
}

// user 无此端点：403。
func TestSSERoleGate(t *testing.T) {
	broker := appevents.NewBroker()
	srv, issued := newSSEServer(t, &fakeAuthorizer{scope: corescope.User("u1")}, &fakeDiag{}, broker, newFakeClock(diagNowTime), newFakeTimer())
	req, _ := http.NewRequest(http.MethodGet, srv.URL+"/api/admin/events", nil)
	req.AddCookie(&http.Cookie{Name: coreauth.InsecureCookieName, Value: issued.Token})
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("do: %v", err)
	}
	defer resp.Body.Close()
	// 无会话是 401；带 user 会话是 403。这里带会话。
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("user 订阅状态 = %d, want 403", resp.StatusCode)
	}
}

// 客户端取消：服务端断流。
func TestSSEClientCancelCloses(t *testing.T) {
	broker := appevents.NewBroker()
	srv, issued := newSSEServer(t, nil, &fakeDiag{}, broker, newFakeClock(diagNowTime), newFakeTimer())
	ctx, cancel := context.WithCancel(context.Background())
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, srv.URL+"/api/admin/events", nil)
	req.AddCookie(&http.Cookie{Name: coreauth.InsecureCookieName, Value: issued.Token})
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("do: %v", err)
	}
	defer resp.Body.Close()
	done := make(chan struct{})
	go func() {
		buf := make([]byte, 1)
		for {
			if _, err := resp.Body.Read(buf); err != nil {
				close(done)
				return
			}
		}
	}()
	cancel()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("取消后服务端未断流")
	}
}
