package assembly

// 限流面装配的失败关闭判据：可信代理表非法即启动失败，不静默忽略。

import (
	"testing"

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
	guard, err := buildRateLimit(&config.Config{RateLimitEnabled: false})
	if err != nil {
		t.Fatalf("buildRateLimit: %v", err)
	}
	if guard != nil {
		t.Fatal("关闭开关时必须返回 nil Guard")
	}
}

func TestBuildRateLimitEnabled(t *testing.T) {
	guard, err := buildRateLimit(&config.Config{
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
