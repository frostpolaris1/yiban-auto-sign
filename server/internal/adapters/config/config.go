// Package config is the only place that reads environment variables.
package config

import (
	"errors"
	"os"
	"strings"
)

// Source is the environment abstraction. Tests pass a map-backed source.
type Source interface {
	Lookup(key string) (string, bool)
}

// Env reads the process environment.
type Env struct{}

// Lookup implements Source.
func (Env) Lookup(key string) (string, bool) { return os.LookupEnv(key) }

// Config is the typed runtime configuration.
type Config struct {
	Env         string
	HTTPAddr    string
	DatabaseURL string
	Timezone    string
}

// Load reads the process environment.
func Load() (*Config, error) { return LoadFrom(Env{}) }

// LoadFrom reads from src. It never reads the process environment itself.
func LoadFrom(src Source) (*Config, error) {
	if src == nil {
		return nil, errors.New("config: nil source")
	}
	cfg := &Config{
		Env:         get(src, "YIBAN_ENV", "development"),
		HTTPAddr:    get(src, "YIBAN_HTTP_ADDR", "127.0.0.1:47183"),
		DatabaseURL: strings.TrimSpace(get(src, "YIBAN_DATABASE_URL", "")),
		Timezone:    get(src, "YIBAN_TZ", "Asia/Shanghai"),
	}
	if cfg.Timezone == "" {
		cfg.Timezone = "Asia/Shanghai"
	}
	return cfg, nil
}

func get(src Source, key, fallback string) string {
	if v, ok := src.Lookup(key); ok && v != "" {
		return v
	}
	return fallback
}
