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
	"log/slog"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
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
