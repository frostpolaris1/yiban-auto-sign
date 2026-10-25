package config

import "testing"

type mapSource map[string]string

func (m mapSource) Lookup(key string) (string, bool) {
	v, ok := m[key]
	return v, ok
}

func TestLoadFromDefaults(t *testing.T) {
	cfg, err := LoadFrom(mapSource{})
	if err != nil {
		t.Fatalf("LoadFrom: %v", err)
	}
	if cfg.Env != "development" || cfg.HTTPAddr != "127.0.0.1:47183" || cfg.Timezone != "Asia/Shanghai" {
		t.Fatalf("unexpected defaults: %#v", cfg)
	}
	if cfg.DatabaseURL != "" {
		t.Fatalf("DatabaseURL = %q, want empty", cfg.DatabaseURL)
	}
}

func TestLoadFromOverrides(t *testing.T) {
	cfg, err := LoadFrom(mapSource{
		"YIBAN_ENV":          "production",
		"YIBAN_HTTP_ADDR":    "127.0.0.1:8080",
		"YIBAN_DATABASE_URL": "  postgres://x  ",
		"YIBAN_TZ":           "UTC",
	})
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
		cfg, err := LoadFrom(mapSource{"YIBAN_ENV": tc.env})
		if err != nil {
			t.Fatalf("%s: %v", tc.env, err)
		}
		if cfg.InsecureCookies != tc.want {
			t.Fatalf("%s: InsecureCookies = %v, want %v", tc.env, cfg.InsecureCookies, tc.want)
		}
	}
}

func TestInsecureCookiesOverride(t *testing.T) {
	cfg, err := LoadFrom(mapSource{"YIBAN_ENV": "staging", "YIBAN_INSECURE_COOKIES": "1"})
	if err != nil {
		t.Fatalf("staging override: %v", err)
	}
	if !cfg.InsecureCookies {
		t.Fatal("override was ignored")
	}

	if _, err := LoadFrom(mapSource{"YIBAN_ENV": "production", "YIBAN_INSECURE_COOKIES": "1"}); err == nil {
		t.Fatal("production must refuse insecure cookies")
	}
	if _, err := LoadFrom(mapSource{"YIBAN_INSECURE_COOKIES": "maybe"}); err == nil {
		t.Fatal("non-boolean flag must be rejected")
	}
}
