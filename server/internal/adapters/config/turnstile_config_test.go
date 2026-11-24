package config

// 人机验证（Cloudflare Turnstile）配置的单元测试。
//
// # 功能
// 钉住三档解析判据：两把密钥必须成对；auto 按密钥齐备落 on/off；
// log/on 缺密钥即启动失败；未知档位拒绝。
// 另钉住 LoadFrom 把三键读进 Config。
//
// # 口径
// 只测本包解析，不测端点；端点行为见 adapters/turnstile。

import (
	"strings"
	"testing"
)

func TestResolveTurnstileMode(t *testing.T) {
	cases := []struct {
		name    string
		mode    string
		siteKey string
		secret  string
		want    string
		wantErr string
	}{
		{name: "auto without keys", mode: "auto", want: TurnstileModeOff},
		{name: "auto with keys", mode: "auto", siteKey: "site", secret: "secret", want: TurnstileModeOn},
		{name: "off with keys", mode: TurnstileModeOff, siteKey: "site", secret: "secret", want: TurnstileModeOff},
		{name: "off without keys", mode: TurnstileModeOff, want: TurnstileModeOff},
		{name: "log with keys", mode: TurnstileModeLog, siteKey: "site", secret: "secret", want: TurnstileModeLog},
		{name: "on with keys", mode: TurnstileModeOn, siteKey: "site", secret: "secret", want: TurnstileModeOn},
		{name: "only site key", mode: "auto", siteKey: "site", wantErr: "must be set together"},
		{name: "only secret", mode: "auto", secret: "secret", wantErr: "must be set together"},
		{name: "log without secret", mode: TurnstileModeLog, wantErr: "requires YIBAN_TURNSTILE_SITE_KEY"},
		{name: "on without secret", mode: TurnstileModeOn, wantErr: "requires YIBAN_TURNSTILE_SITE_KEY"},
		{name: "unknown mode", mode: "sometimes", wantErr: "unknown turnstile mode"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := resolveTurnstileMode(tc.mode, tc.siteKey, tc.secret)
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("resolveTurnstileMode(%q, %q, %q) = %q, %v; want error containing %q",
						tc.mode, tc.siteKey, tc.secret, got, err, tc.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatalf("resolveTurnstileMode(%q, %q, %q): %v", tc.mode, tc.siteKey, tc.secret, err)
			}
			if got != tc.want {
				t.Fatalf("mode = %q, want %q", got, tc.want)
			}
		})
	}
}

// 三键读进 Config：缺省 auto 无密钥落 off，配齐落 on。
func TestLoadFromTurnstileKeys(t *testing.T) {
	cfg, err := LoadFrom(baseSource())
	if err != nil {
		t.Fatalf("LoadFrom defaults: %v", err)
	}
	if cfg.TurnstileMode != TurnstileModeOff || cfg.TurnstileSiteKey != "" || cfg.TurnstileSecret != "" {
		t.Fatalf("default turnstile config = %q/%q/%q, want off and empty keys",
			cfg.TurnstileMode, cfg.TurnstileSiteKey, cfg.TurnstileSecret)
	}

	src := baseSource()
	src["YIBAN_TURNSTILE_MODE"] = " auto "
	src["YIBAN_TURNSTILE_SITE_KEY"] = " site-key "
	src["YIBAN_TURNSTILE_SECRET"] = " secret "
	cfg, err = LoadFrom(src)
	if err != nil {
		t.Fatalf("LoadFrom turnstile: %v", err)
	}
	if cfg.TurnstileMode != TurnstileModeOn || cfg.TurnstileSiteKey != "site-key" || cfg.TurnstileSecret != "secret" {
		t.Fatalf("turnstile config = %q/%q/%q, want on and trimmed keys",
			cfg.TurnstileMode, cfg.TurnstileSiteKey, cfg.TurnstileSecret)
	}

	partial := baseSource()
	partial["YIBAN_TURNSTILE_SITE_KEY"] = "site-key"
	if _, err := LoadFrom(partial); err == nil {
		t.Fatal("LoadFrom accepted a site key without a secret")
	}
}
