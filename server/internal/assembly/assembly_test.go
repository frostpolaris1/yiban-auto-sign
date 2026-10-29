package assembly

// Run 的「一次装配」不变量 e2e（派单 P7 F1）。
//
// # 功能
// 按生产入口启动进程，短延时后取消 ctx 模拟 SIGTERM。
// 断言一次 Run 只装配一次：
//   1. buildStack 计数恰为 1（结构性断言）；
//   2. 「无数据库」警告只打一次（行为断言，不看走哪条调用路径）。
// 双装配会让执行循环与 HTTP 各持一份 Stack：worker 事件进第一份 broker，
// SSE 订阅第二份，管理员永远收不到任务事件。
//
// # 口径
// 无数据库启动：不连真库、不占固定端口（HTTPAddr=127.0.0.1:0）。
// t.Setenv 与包级接缝替换互斥并行：本文件不调 t.Parallel。

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
	appworker "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/worker"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
)

// countingHandler 记录全部日志消息，供重复装配断言使用。
type countingHandler struct {
	mu   sync.Mutex
	msgs []string
}

func (h *countingHandler) Enabled(context.Context, slog.Level) bool { return true }

func (h *countingHandler) Handle(_ context.Context, r slog.Record) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.msgs = append(h.msgs, r.Message)
	return nil
}

func (h *countingHandler) WithAttrs([]slog.Attr) slog.Handler { return h }

func (h *countingHandler) WithGroup(string) slog.Handler { return h }

// count 返回含 sub 的消息条数。
func (h *countingHandler) count(sub string) int {
	h.mu.Lock()
	defer h.mu.Unlock()
	n := 0
	for _, m := range h.msgs {
		if strings.Contains(m, sub) {
			n++
		}
	}
	return n
}

func TestRunAssemblesOnce(t *testing.T) {
	t.Setenv("YIBAN_ENV", "test")
	t.Setenv("YIBAN_HTTP_ADDR", "127.0.0.1:0")
	t.Setenv("YIBAN_DATABASE_URL", "")
	t.Setenv("YIBAN_HASH_PEPPER", "0123456789abcdef0123456789abcdef")
	t.Setenv("YIBAN_CREDENTIAL_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
	t.Setenv("YIBAN_PHOTO_DIR", t.TempDir())

	var calls int32
	orig := buildStack
	buildStack = func(cfg *config.Config, logger *slog.Logger) (*Stack, error) {
		atomic.AddInt32(&calls, 1)
		return orig(cfg, logger)
	}
	t.Cleanup(func() { buildStack = orig })

	handler := &countingHandler{}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- Run(ctx, slog.New(handler)) }()

	time.Sleep(100 * time.Millisecond)
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("Run 返回错误: %v", err)
		}
	case <-time.After(30 * time.Second):
		t.Fatal("ctx 取消后 Run 未返回")
	}

	if got := atomic.LoadInt32(&calls); got != 1 {
		t.Fatalf("栈装配次数 = %d，want 1（0=未经单装配接缝，>1=双装配）", got)
	}
	// 「无数据库」警告由 Build 打印；双装配必然打两遍。
	// 该断言不看调用路径，接缝被绕过也能抓住。
	const warn = "YIBAN_DATABASE_URL is empty"
	if got := handler.count(warn); got != 2 {
		t.Fatalf("「%s」警告 %d 条，want 2（Build 打两条不同警告各一次：auth 一条、501 一条）", warn, got)
	}
	if got := handler.count("auth endpoints are disabled"); got != 1 {
		t.Fatalf("认证禁用警告 %d 条，want 1：一次 Run 只允许一次装配", got)
	}
}

// TestBuildWithoutDatabaseServesHealthz 是装配冒烟（派单 F19）。
// 无数据库是受支持的启动形态：Engine 必须可用、Worker 必须为 nil、
// Close 可重复调用不 panic，且 /healthz 应答 200。
func TestBuildWithoutDatabaseServesHealthz(t *testing.T) {
	cfg := &config.Config{
		Env:           "test",
		HTTPAddr:      "127.0.0.1:0",
		Timezone:      config.DefaultTimezone,
		DatabaseURL:   "",
		HashPepper:    []byte("0123456789abcdef0123456789abcdef"),
		CredentialKey: make([]byte, 32),
		PhotoDir:      t.TempDir(),
	}
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	stack, err := Build(cfg, logger)
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	if stack == nil {
		t.Fatal("Build 返回 nil Stack")
	}
	if stack.Engine == nil {
		t.Fatal("Engine 不得为 nil：无数据库时认证端点回 501，但路由必须存在")
	}
	if stack.Worker != nil {
		t.Fatal("无数据库时 Worker 必须为 nil（执行循环无库可跑）")
	}
	if stack.Close == nil {
		t.Fatal("Close 不得为 nil")
	}

	srv := httptest.NewServer(stack.Engine)
	defer srv.Close()
	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatalf("GET /healthz: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("/healthz = %d, want 200", resp.StatusCode)
	}
	var body struct {
		Status string `json:"status"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode healthz: %v", err)
	}
	if body.Status != "ok" {
		t.Fatalf("healthz status = %q, want ok", body.Status)
	}

	// Close 幂等：srv.Shutdown 与显式收尾都会调它。
	stack.Close()
	stack.Close()
}

// fakeLoop 是执行循环替身：只记录 Run 有没有启动和停止它。
type fakeLoop struct {
	started bool
	stopped bool
}

func (f *fakeLoop) Start() error { f.started = true; return nil }

func (f *fakeLoop) Stop(context.Context) (int64, error) {
	f.stopped = true
	return 0, nil
}

func (f *fakeLoop) Err() error { return nil }

// TestRunStartsAndStopsWorkerLoop 钉住 Run 的启动块（派单 P2-2）。
// 删掉 `if stack.Worker != nil` 整块时，假循环收不到 Start/Stop，本测红。
// 装配用假 Stack：Worker 非 nil 但方法不会被调用（假循环不碰它）。
func TestRunStartsAndStopsWorkerLoop(t *testing.T) {
	t.Setenv("YIBAN_ENV", "test")
	t.Setenv("YIBAN_HTTP_ADDR", "127.0.0.1:0")
	t.Setenv("YIBAN_DATABASE_URL", "")
	t.Setenv("YIBAN_HASH_PEPPER", "0123456789abcdef0123456789abcdef")
	t.Setenv("YIBAN_CREDENTIAL_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
	t.Setenv("YIBAN_PHOTO_DIR", t.TempDir())

	fake := &fakeLoop{}
	origLoop, origBuild := newLoop, buildStack
	newLoop = func(*Stack, time.Duration, int) loopHandle { return fake }
	buildStack = func(*config.Config, *slog.Logger) (*Stack, error) {
		return &Stack{
			Engine: http.NewServeMux(),
			Worker: &appworker.Service{},
			Close:  func() {},
		}, nil
	}
	t.Cleanup(func() { newLoop, buildStack = origLoop, origBuild })

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- Run(ctx, slog.New(slog.NewTextHandler(io.Discard, nil))) }()

	time.Sleep(100 * time.Millisecond)
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("Run 返回错误: %v", err)
		}
	case <-time.After(30 * time.Second):
		t.Fatal("ctx 取消后 Run 未返回")
	}
	if !fake.started {
		t.Fatal("Run 必须启动执行循环：Worker 非 nil 时删掉启动块会静默无循环")
	}
	if !fake.stopped {
		t.Fatal("ctx 结束后 Run 必须停止执行循环")
	}
}

// TestRunClosesPoolAfterWorkerDrain 钉住 Run 的停机顺序（工单 6jk）。
//
// 不变式：连接池不得在 worker 在飞结算完成前关闭。
// 旧形状 srv.RegisterOnShutdown(stack.Close) 让 srv.Shutdown 与 loop.Stop
// 并行关池：CountRunning 报 closed pool，在飞清零失败。
// 本测走 Run 的真实收尾路径（ctx 取消），用协调通道把竞态变成确定性判据：
//   - 修复后：Stop 等 100ms 仍未见关池 → sawClosed=false，收尾后才关池；
//   - 旧形状：Shutdown 立刻并行关池 → Stop 期间看到关池 → 红。
func TestRunClosesPoolAfterWorkerDrain(t *testing.T) {
	t.Setenv("YIBAN_ENV", "test")
	t.Setenv("YIBAN_HTTP_ADDR", "127.0.0.1:0")
	t.Setenv("YIBAN_DATABASE_URL", "")
	t.Setenv("YIBAN_HASH_PEPPER", "0123456789abcdef0123456789abcdef")
	t.Setenv("YIBAN_CREDENTIAL_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
	t.Setenv("YIBAN_PHOTO_DIR", t.TempDir())

	var closed atomic.Bool
	closedCh := make(chan struct{})
	loop := &closeWatchingLoop{closedCh: closedCh}
	origLoop, origBuild := newLoop, buildStack
	newLoop = func(*Stack, time.Duration, int) loopHandle { return loop }
	buildStack = func(*config.Config, *slog.Logger) (*Stack, error) {
		return &Stack{
			Engine: http.NewServeMux(),
			Worker: &appworker.Service{},
			Close: func() {
				if closed.CompareAndSwap(false, true) {
					close(closedCh)
				}
			},
		}, nil
	}
	t.Cleanup(func() { newLoop, buildStack = origLoop, origBuild })

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- Run(ctx, slog.New(slog.NewTextHandler(io.Discard, nil))) }()
	time.Sleep(100 * time.Millisecond)
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("Run 返回错误: %v", err)
		}
	case <-time.After(30 * time.Second):
		t.Fatal("ctx 取消后 Run 未返回")
	}
	if loop.sawClosed.Load() {
		t.Fatal("连接池在 worker 结算完成前被关闭：CountRunning 会报 closed pool")
	}
	if !closed.Load() {
		t.Fatal("Run 收尾后必须关闭连接池")
	}
}

// closeWatchingLoop 在 Stop 期间观察连接池是否被关闭（最多 100ms）。
type closeWatchingLoop struct {
	closedCh  <-chan struct{}
	sawClosed atomic.Bool
}

func (l *closeWatchingLoop) Start() error { return nil }

func (l *closeWatchingLoop) Stop(context.Context) (int64, error) {
	select {
	case <-l.closedCh:
		l.sawClosed.Store(true)
	case <-time.After(100 * time.Millisecond):
	}
	return 0, nil
}

func (l *closeWatchingLoop) Err() error { return nil }

// TestNewServerClosesPoolOnHTTPShutdown 钉住 NewServer 的既有契约：
// 调用方没有显式收尾，Shutdown 必须释放连接池。
func TestNewServerClosesPoolOnHTTPShutdown(t *testing.T) {
	var closed atomic.Bool
	stack := &Stack{Engine: http.NewServeMux(), Close: func() { closed.Store(true) }}
	srv := newServerFromStack(&config.Config{HTTPAddr: "127.0.0.1:0"}, stack, true)
	if err := srv.Shutdown(context.Background()); err != nil {
		t.Fatalf("Shutdown: %v", err)
	}
	// Shutdown 用 goroutine 跑 onShutdown，给它一个调度窗口。
	// 时刻必须走 clock 包：canonical 的 clock.now 规则管到 _test.go，
	// `server/scripts/verify.sh` 会以 -tests 跑该门。
	clk := clock.System{}
	deadline := clk.Now().Add(time.Second)
	for !closed.Load() && clk.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if !closed.Load() {
		t.Fatal("NewServer 的 server 在 Shutdown 后必须释放连接池")
	}
}
