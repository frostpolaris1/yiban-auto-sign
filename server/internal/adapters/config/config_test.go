package config

import (
	"encoding/base64"
	"testing"
)

type mapSource map[string]string

func (m mapSource) Lookup(key string) (string, bool) {
	v, ok := m[key]
	return v, ok
}

const (
	// validPepper 是 32 字符的合法 pepper。
	validPepper = "0123456789abcdef0123456789abcdef"
	// validCredentialKey 是 32 字节钥的 base64 文本。
	validCredentialKey = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
	// longCredentialKey 解码后 33 字节，比 AES-256 的键长多一字节。
	longCredentialKey = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWZY"
)

// baseSource 是只含三个必填项的最小环境。
func baseSource() mapSource {
	return mapSource{
		"YIBAN_ENV":            "development",
		"YIBAN_HASH_PEPPER":    validPepper,
		"YIBAN_CREDENTIAL_KEY": validCredentialKey,
	}
}

func TestLoadFromRequiresEnv(t *testing.T) {
	for _, src := range []mapSource{{}, {"YIBAN_ENV": ""}, {"YIBAN_ENV": "   "}} {
		if _, err := LoadFrom(src); err == nil {
			t.Fatalf("source %#v must be rejected", src)
		}
	}
}

func TestLoadFromDefaults(t *testing.T) {
	cfg, err := LoadFrom(baseSource())
	if err != nil {
		t.Fatalf("LoadFrom: %v", err)
	}
	if cfg.Env != "development" || cfg.HTTPAddr != "127.0.0.1:47183" || cfg.Timezone != "Asia/Shanghai" {
		t.Fatalf("unexpected defaults: %#v", cfg)
	}
	if cfg.DatabaseURL != "" {
		t.Fatalf("DatabaseURL = %q, want empty", cfg.DatabaseURL)
	}
	if !cfg.InsecureCookies {
		t.Fatal("development must default to insecure cookies")
	}
}

func TestLoadFromOverrides(t *testing.T) {
	src := baseSource()
	src["YIBAN_ENV"] = "production"
	src["YIBAN_HTTP_ADDR"] = "127.0.0.1:8080"
	src["YIBAN_DATABASE_URL"] = "  postgres://x  "
	src["YIBAN_TZ"] = "UTC"
	cfg, err := LoadFrom(src)
	if err != nil {
		t.Fatalf("LoadFrom: %v", err)
	}
	if cfg.Env != "production" || cfg.HTTPAddr != "127.0.0.1:8080" ||
		cfg.DatabaseURL != "postgres://x" || cfg.Timezone != "UTC" {
		t.Fatalf("unexpected overrides: %#v", cfg)
	}
}

func TestLoadFromNil(t *testing.T) {
	if _, err := LoadFrom(nil); err == nil {
		t.Fatal("LoadFrom(nil) must fail")
	}
}

func TestLoadFromRejectsUnknownEnv(t *testing.T) {
	for _, env := range []string{"prod", "Production", "dev"} {
		if _, err := LoadFrom(mapSource{"YIBAN_ENV": env}); err == nil {
			t.Fatalf("env %q must be rejected", env)
		}
	}
}

func TestInsecureCookiesFollowEnv(t *testing.T) {
	cases := []struct {
		env  string
		want bool
	}{
		{"development", true},
		{"test", true},
		{"staging", false},
		{"production", false},
	}
	for _, tc := range cases {
		src := baseSource()
		src["YIBAN_ENV"] = tc.env
		cfg, err := LoadFrom(src)
		if err != nil {
			t.Fatalf("%s: %v", tc.env, err)
		}
		if cfg.InsecureCookies != tc.want {
			t.Fatalf("%s: InsecureCookies = %v, want %v", tc.env, cfg.InsecureCookies, tc.want)
		}
	}
}

func TestInsecureCookiesOverride(t *testing.T) {
	src := baseSource()
	src["YIBAN_ENV"] = "staging"
	src["YIBAN_INSECURE_COOKIES"] = "1"
	cfg, err := LoadFrom(src)
	if err != nil {
		t.Fatalf("staging override: %v", err)
	}
	if !cfg.InsecureCookies {
		t.Fatal("override was ignored")
	}

	prod := baseSource()
	prod["YIBAN_ENV"] = "production"
	prod["YIBAN_INSECURE_COOKIES"] = "1"
	if _, err := LoadFrom(prod); err == nil {
		t.Fatal("production must refuse insecure cookies")
	}

	bad := baseSource()
	bad["YIBAN_INSECURE_COOKIES"] = "maybe"
	if _, err := LoadFrom(bad); err == nil {
		t.Fatal("non-boolean flag must be rejected")
	}
}

func TestLoadFromRequiresHashPepper(t *testing.T) {
	for name, src := range map[string]mapSource{
		"missing":   {"YIBAN_ENV": "development", "YIBAN_CREDENTIAL_KEY": validCredentialKey},
		"empty":     {"YIBAN_ENV": "development", "YIBAN_HASH_PEPPER": "  ", "YIBAN_CREDENTIAL_KEY": validCredentialKey},
		"too short": {"YIBAN_ENV": "development", "YIBAN_HASH_PEPPER": "short", "YIBAN_CREDENTIAL_KEY": validCredentialKey},
	} {
		if _, err := LoadFrom(src); err == nil {
			t.Fatalf("%s pepper must be rejected", name)
		}
	}
	cfg, err := LoadFrom(baseSource())
	if err != nil {
		t.Fatalf("valid pepper: %v", err)
	}
	if string(cfg.HashPepper) != validPepper {
		t.Fatalf("pepper = %q", cfg.HashPepper)
	}
	// 首尾空白是输入事故，不是熵：
	// 存储值取裁剪后的形式，与 DatabaseURL 的约定一致。
	padded := baseSource()
	padded["YIBAN_HASH_PEPPER"] = "  0123456789abcdef  "
	cfg, err = LoadFrom(padded)
	if err != nil {
		t.Fatalf("padded pepper: %v", err)
	}
	if len(cfg.HashPepper) != 16 || string(cfg.HashPepper) != "0123456789abcdef" {
		t.Fatalf("padded pepper stored %q", cfg.HashPepper)
	}
}

// TestLoadFromCredentialKeyIsBase64Of32Bytes 钉住凭据钥的形状。
//
// 凭据钥独立于 pepper：轮换 pepper 只让摘要与审计链失效，
// 不能让凭据永久不可解密（W2C-R2-04）。
// 形状错即启动错误：短钥进 AES-256-GCM 会直接报错，
// 长度对但内容错的钥会静默加密出无法解密的行。
func TestLoadFromCredentialKeyIsBase64Of32Bytes(t *testing.T) {
	for name, key := range map[string]string{
		"missing":        "",
		"blank":          "   ",
		"not base64":     "not base64!",
		"too short":      base64.StdEncoding.EncodeToString([]byte("short")),
		"too long":       longCredentialKey,
		"pepper literal": validPepper,
	} {
		src := baseSource()
		if key == "" {
			delete(src, "YIBAN_CREDENTIAL_KEY")
		} else {
			src["YIBAN_CREDENTIAL_KEY"] = key
		}
		if _, err := LoadFrom(src); err == nil {
			t.Fatalf("%s credential key must be rejected", name)
		}
	}

	cfg, err := LoadFrom(baseSource())
	if err != nil {
		t.Fatalf("valid credential key: %v", err)
	}
	if len(cfg.CredentialKey) != 32 {
		t.Fatalf("credential key length = %d, want 32", len(cfg.CredentialKey))
	}
	if got := base64.StdEncoding.EncodeToString(cfg.CredentialKey); got != validCredentialKey {
		t.Fatalf("credential key = %q, want the decoded input", got)
	}
}

// 调度参数：缺省值来自名册；越界或形状错即启动错误。
func TestScheduleConfigDefaultsAndBounds(t *testing.T) {
	cfg, err := LoadFrom(baseSource())
	if err != nil {
		t.Fatalf("LoadFrom: %v", err)
	}
	if cfg.Workers != 1 || cfg.AvgAttempt.Seconds() != 3 || cfg.EgressRate != 1.0 {
		t.Fatalf("调度缺省 = workers %d avg %v rate %v", cfg.Workers, cfg.AvgAttempt, cfg.EgressRate)
	}
	if cfg.EdgeFront.Seconds() != 60 || cfg.EdgeBack.Seconds() != 60 {
		t.Fatalf("边缘缺省 = front %v back %v", cfg.EdgeFront, cfg.EdgeBack)
	}
	if cfg.AllowTimePref || cfg.TimePrefCooldown.Seconds() != 30 {
		t.Fatalf("自选缺省 = allow %v cooldown %v", cfg.AllowTimePref, cfg.TimePrefCooldown)
	}
	if cfg.PhotoDir != "data/photos" {
		t.Fatalf("PhotoDir = %q, want data/photos", cfg.PhotoDir)
	}

	src := baseSource()
	src["YIBAN_WORKERS"] = "4"
	src["YIBAN_AVG_ATTEMPT_SEC"] = "5"
	src["YIBAN_EGRESS_RATE"] = "1.5"
	src["YIBAN_WINDOW_EDGE_FRONT_SEC"] = "30"
	src["YIBAN_WINDOW_EDGE_BACK_SEC"] = "0"
	src["YIBAN_ALLOW_TIME_PREF"] = "true"
	src["YIBAN_TIME_PREF_COOLDOWN_SEC"] = "60"
	src["YIBAN_PHOTO_DIR"] = "var/photos"
	cfg, err = LoadFrom(src)
	if err != nil {
		t.Fatalf("LoadFrom overrides: %v", err)
	}
	if cfg.Workers != 4 || cfg.AvgAttempt.Seconds() != 5 || cfg.EgressRate != 1.5 ||
		cfg.EdgeFront.Seconds() != 30 || cfg.EdgeBack != 0 ||
		!cfg.AllowTimePref || cfg.TimePrefCooldown.Seconds() != 60 ||
		cfg.PhotoDir != "var/photos" {
		t.Fatalf("覆盖值未生效: %#v", cfg)
	}

	bad := []struct {
		key, value string
	}{
		{"YIBAN_WORKERS", "0"},
		{"YIBAN_WORKERS", "65"},
		{"YIBAN_WORKERS", "many"},
		{"YIBAN_AVG_ATTEMPT_SEC", "0"},
		{"YIBAN_AVG_ATTEMPT_SEC", "301"},
		{"YIBAN_EGRESS_RATE", "0.1"},
		{"YIBAN_EGRESS_RATE", "4.1"},
		{"YIBAN_EGRESS_RATE", "fast"},
		{"YIBAN_WINDOW_EDGE_FRONT_SEC", "301"},
		{"YIBAN_WINDOW_EDGE_BACK_SEC", "-1"},
		{"YIBAN_ALLOW_TIME_PREF", "yes"},
		{"YIBAN_TIME_PREF_COOLDOWN_SEC", "3601"},
		{"YIBAN_PHOTO_DIR", "   "},
	}
	for _, c := range bad {
		src := baseSource()
		src[c.key] = c.value
		if _, err := LoadFrom(src); err == nil {
			t.Fatalf("%s=%q 必须被拒绝", c.key, c.value)
		}
	}
}
