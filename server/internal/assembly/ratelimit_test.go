package assembly

// 限流面装配的失败关闭判据：
//   - 可信代理表非法即启动失败，不静默忽略；
//   - production 关闭开关即启动失败：路由层把 nil Guard 视为不限流，
//     静默关闭等于全部入站限速消失；非 production 仍可关闭。

import (
	"io"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
)

func TestBuildRateLimitRejectsBadProxies(t *testing.T) {
	cfg := &config.Config{
		RateLimitEnabled:  true,
		RateIPPerMin:      60,
		RateIPBurst:       20,
		RateAccountPerMin: 10,
		RateAccountBurst:  5,
		TrustedProxies:    "not-a-cidr",
	}
	if _, err := buildRateLimit(cfg); err == nil {
		t.Fatal("非法可信代理表必须让启动失败")
	}
}

func TestBuildRateLimitDisabled(t *testing.T) {
	guard, err := buildRateLimit(&config.Config{Env: "development", RateLimitEnabled: false})
	if err != nil {
		t.Fatalf("buildRateLimit: %v", err)
	}
	if guard != nil {
		t.Fatal("关闭开关时必须返回 nil Guard")
	}
}

func TestBuildRateLimitEnabled(t *testing.T) {
	guard, err := buildRateLimit(&config.Config{
		Env:               "production",
		RateLimitEnabled:  true,
		RateIPPerMin:      60,
		RateIPBurst:       20,
		RateAccountPerMin: 10,
		RateAccountBurst:  5,
		TrustedProxies:    "127.0.0.1, 10.0.0.0/8",
	})
	if err != nil {
		t.Fatalf("buildRateLimit: %v", err)
	}
	if guard == nil || guard.IP == nil || guard.Account == nil {
		t.Fatal("开启时必须给出两道路径")
	}
	if len(guard.Trusted) != 2 {
		t.Fatalf("可信代理 = %d, want 2", len(guard.Trusted))
	}
}

// production 关闭入站限流必须启动失败：nil Guard 在路由层等于不限流，
// 误配 YIBAN_RATE_LIMIT_ENABLE=false 会静默关掉全部入站限速。
// 错误必须点名该键，运维才能从启动日志直接看到改哪个变量。
func TestBuildRateLimitRejectsDisabledInProduction(t *testing.T) {
	guard, err := buildRateLimit(&config.Config{Env: "production", RateLimitEnabled: false})
	if err == nil {
		t.Fatal("production 关闭入站限流必须让启动失败")
	}
	if guard != nil {
		t.Fatal("启动失败时不得返回 Guard")
	}
	if !strings.Contains(err.Error(), "YIBAN_RATE_LIMIT_ENABLE") {
		t.Fatalf("错误必须点名 YIBAN_RATE_LIMIT_ENABLE: %v", err)
	}
}

// production 之外仍可关闭：本地开发、e2e 与压测要能关掉限速。
func TestBuildRateLimitDisabledOutsideProduction(t *testing.T) {
	for _, env := range []string{"development", "test", "staging"} {
		t.Run(env, func(t *testing.T) {
			guard, err := buildRateLimit(&config.Config{Env: env, RateLimitEnabled: false})
			if err != nil {
				t.Fatalf("%s 关闭限流: %v", env, err)
			}
			if guard != nil {
				t.Fatalf("%s 关闭限流时必须返回 nil Guard", env)
			}
		})
	}
}

// 启动门：production + 关闭必须从 Build 就失败，而不是只让 helper 红。
// buildRateLimit 在 Build 打开迁移与连接池之前调用，故最小配置即可判启动
// 失败，不依赖真库、端口或其他必填项——这正是「启动即拒」的接线证据。
func TestBuildRejectsDisabledRateLimitInProduction(t *testing.T) {
	cfg := &config.Config{
		Env:              "production",
		RateLimitEnabled: false,
	}
	stack, err := Build(cfg, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err == nil {
		if stack != nil {
			stack.Close()
		}
		t.Fatal("production 关闭限流时 Build 必须失败")
	}
	if stack != nil {
		t.Fatal("Build 失败时不得返回半装配 Stack")
	}
	if !strings.Contains(err.Error(), "YIBAN_RATE_LIMIT_ENABLE") {
		t.Fatalf("启动错误必须点名 YIBAN_RATE_LIMIT_ENABLE: %v", err)
	}
}

// 测试邮件冷却必须咬住「每管理员 60s 一封」。
// burst 改成 2 时第二次会放行；PerMinute 调高时等待会明显小于 60s。
func TestMailTestThrottleIsOnePerAdminPerMinute(t *testing.T) {
	throttle, err := buildMailTestThrottle()
	if err != nil {
		t.Fatalf("buildMailTestThrottle: %v", err)
	}
	if ok, _ := throttle.Allow("admin-1"); !ok {
		t.Fatal("第一次测试邮件被拒绝")
	}
	ok, wait := throttle.Allow("admin-1")
	if ok {
		t.Fatal("同一管理员 60s 内第二次测试邮件被放行")
	}
	if wait < 55*time.Second || wait > 61*time.Second {
		t.Fatalf("冷却等待 = %v, want 约 60s", wait)
	}
	if ok, _ := throttle.Allow("admin-2"); !ok {
		t.Fatal("不同管理员共用冷却桶")
	}
}

// publicOrigin 把公开基址收敛成 scheme://host[:port]；解析失败返回空串。
func TestPublicOrigin(t *testing.T) {
	cases := map[string]string{
		"https://app.example.test/":       "https://app.example.test",
		"http://127.0.0.1:47183":          "http://127.0.0.1:47183",
		"https://app.example.test/path/x": "https://app.example.test",
		"":                                "",
		"not a url":                       "",
	}
	for raw, want := range cases {
		if got := publicOrigin(raw); got != want {
			t.Fatalf("publicOrigin(%q) = %q, want %q", raw, got, want)
		}
	}
}
