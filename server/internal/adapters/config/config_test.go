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
