package config

// 入站限流与 KDF 闸的配置判据：域内接受、越界拒绝、缺省值稳定。
// 名册与代码域的逐键相等由 registry_parity_test 覆盖；这里钉住读取行为。

import "testing"

func TestRateLimitDefaults(t *testing.T) {
	cfg, err := LoadFrom(baseSource())
	if err != nil {
		t.Fatalf("LoadFrom: %v", err)
	}
	if !cfg.RateLimitEnabled {
		t.Fatal("限流缺省必须开启")
	}
	if cfg.RateIPPerMin != 60 || cfg.RateIPBurst != 20 {
		t.Fatalf("IP 阈值 = %d/%d, want 60/20", cfg.RateIPPerMin, cfg.RateIPBurst)
	}
	if cfg.RateAccountPerMin != 10 || cfg.RateAccountBurst != 5 {
		t.Fatalf("账号阈值 = %d/%d, want 10/5", cfg.RateAccountPerMin, cfg.RateAccountBurst)
	}
	if cfg.TrustedProxies != "" {
		t.Fatalf("可信代理缺省 = %q, want 空（不信任任何转发头）", cfg.TrustedProxies)
	}
	if cfg.KDFConcurrency != 4 {
		t.Fatalf("KDF 并发缺省 = %d, want 4", cfg.KDFConcurrency)
	}
	if cfg.MaxAccountsPerOwner != 20 {
		t.Fatalf("每属主账号上限缺省 = %d, want 20", cfg.MaxAccountsPerOwner)
	}
}

func TestRateLimitBoundsRejected(t *testing.T) {
	cases := []struct{ key, value string }{
		{"YIBAN_RATE_IP_PER_MIN", "0"},
		{"YIBAN_RATE_IP_PER_MIN", "6001"},
		{"YIBAN_RATE_IP_BURST", "0"},
		{"YIBAN_RATE_ACCOUNT_PER_MIN", "601"},
		{"YIBAN_RATE_ACCOUNT_BURST", "0"},
		{"YIBAN_KDF_CONCURRENCY", "0"},
		{"YIBAN_KDF_CONCURRENCY", "65"},
		{"YIBAN_MAX_ACCOUNTS_PER_OWNER", "0"},
		{"YIBAN_MAX_ACCOUNTS_PER_OWNER", "1001"},
	}
	for _, c := range cases {
		src := baseSource()
		src[c.key] = c.value
		if _, err := LoadFrom(src); err == nil {
			t.Fatalf("%s=%q 必须被拒绝", c.key, c.value)
		}
	}
}

func TestRateLimitValuesAccepted(t *testing.T) {
	src := baseSource()
	src["YIBAN_RATE_LIMIT_ENABLE"] = "0"
	src["YIBAN_RATE_IP_PER_MIN"] = "120"
	src["YIBAN_RATE_IP_BURST"] = "30"
	src["YIBAN_RATE_ACCOUNT_PER_MIN"] = "20"
	src["YIBAN_RATE_ACCOUNT_BURST"] = "6"
	src["YIBAN_TRUSTED_PROXIES"] = " 10.0.0.0/8 , 127.0.0.1 "
	src["YIBAN_KDF_CONCURRENCY"] = "2"
	src["YIBAN_MAX_ACCOUNTS_PER_OWNER"] = "50"
	cfg, err := LoadFrom(src)
	if err != nil {
		t.Fatalf("LoadFrom: %v", err)
	}
	if cfg.RateLimitEnabled {
		t.Fatal("YIBAN_RATE_LIMIT_ENABLE=0 必须关闭")
	}
	if cfg.RateIPPerMin != 120 || cfg.RateIPBurst != 30 ||
		cfg.RateAccountPerMin != 20 || cfg.RateAccountBurst != 6 {
		t.Fatalf("阈值 = %d/%d/%d/%d", cfg.RateIPPerMin, cfg.RateIPBurst,
			cfg.RateAccountPerMin, cfg.RateAccountBurst)
	}
	if cfg.TrustedProxies != "10.0.0.0/8 , 127.0.0.1" {
		t.Fatalf("可信代理 = %q，want 去首尾空白后的原值", cfg.TrustedProxies)
	}
	if cfg.KDFConcurrency != 2 {
		t.Fatalf("KDF 并发 = %d, want 2", cfg.KDFConcurrency)
	}
	if cfg.MaxAccountsPerOwner != 50 {
		t.Fatalf("每属主账号上限 = %d, want 50", cfg.MaxAccountsPerOwner)
	}
}
