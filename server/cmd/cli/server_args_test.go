package main

// server 子命令的参数门（派单 P3）。
//
// # 功能
// `yiban-cli server` 没有选项：多余参数必须报错退出 2，
// 不能静默忽略（把 `--post 8080` 当无事发生会进生产）。
//
// # 口径
// 转调经 runAssembly 接缝注入：测试不启动真实服务、不读真实配置。

import (
	"context"
	"log/slog"
	"testing"
)

func TestRunServerRejectsUnexpectedArgs(t *testing.T) {
	orig := runAssembly
	t.Cleanup(func() { runAssembly = orig })
	runAssembly = func(context.Context, *slog.Logger) error {
		t.Fatal("多余参数必须在装配前拒绝")
		return nil
	}
	if code := runServer([]string{"--post", "8080"}); code != 2 {
		t.Fatalf("多余参数 code = %d, want 2", code)
	}
}

func TestRunServerDelegatesWithoutArgs(t *testing.T) {
	orig := runAssembly
	t.Cleanup(func() { runAssembly = orig })
	var called bool
	runAssembly = func(context.Context, *slog.Logger) error {
		called = true
		return nil
	}
	if code := runServer(nil); code != 0 || !called {
		t.Fatalf("无参数必须转调装配：code=%d called=%v", code, called)
	}
}
