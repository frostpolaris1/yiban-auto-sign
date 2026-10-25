// Package config is the only place that reads environment variables.
package config

import (
	"errors"
	"fmt"
	"os"
	"strconv"
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
	Env             string
	HTTPAddr        string
	DatabaseURL     string
	Timezone        string
	InsecureCookies bool
	// HashPepper keys the HMAC digests of IP addresses and user agents. It is
	// required: a deployment without one would silently lose its abuse trail.
	HashPepper []byte
}

// knownEnvs is a closed set. An unknown value is a startup error: a typo such
// as "prod" must not silently select the development cookie name.
var knownEnvs = map[string]bool{
	"development": true,
	"test":        true,
	"staging":     true,
	"production":  true,
}

// Load reads the process environment.
func Load() (*Config, error) { return LoadFrom(Env{}) }

// LoadFrom reads from src. It never reads the process environment itself.
func LoadFrom(src Source) (*Config, error) {
	if src == nil {
		return nil, errors.New("config: nil source")
	}
	rawEnv, ok := src.Lookup("YIBAN_ENV")
	env := strings.TrimSpace(rawEnv)
	if !ok || env == "" {
		return nil, errors.New("config: YIBAN_ENV is required (development, test, staging or production)")
	}
	if !knownEnvs[env] {
		return nil, fmt.Errorf("config: unknown YIBAN_ENV %q (want development, test, staging or production)", env)
	}
	cfg := &Config{
		Env:         env,
		HTTPAddr:    get(src, "YIBAN_HTTP_ADDR", "127.0.0.1:47183"),
		DatabaseURL: strings.TrimSpace(get(src, "YIBAN_DATABASE_URL", "")),
		Timezone:    get(src, "YIBAN_TZ", "Asia/Shanghai"),
		// Local HTTP cannot set a Secure cookie, so the unprefixed name is
		// the default only for development and test.
		InsecureCookies: env == "development" || env == "test",
	}
	if cfg.Timezone == "" {
		cfg.Timezone = "Asia/Shanghai"
	}
	if raw, ok := src.Lookup("YIBAN_INSECURE_COOKIES"); ok && raw != "" {
		flag, err := strconv.ParseBool(raw)
		if err != nil {
			return nil, fmt.Errorf("config: YIBAN_INSECURE_COOKIES must be a boolean: %w", err)
		}
		cfg.InsecureCookies = flag
	}
	if env == "production" && cfg.InsecureCookies {
		return nil, errors.New("config: production refuses insecure cookies")
	}
	rawPepper, pepperSet := src.Lookup("YIBAN_HASH_PEPPER")
	if !pepperSet || len(strings.TrimSpace(rawPepper)) < 16 {
		return nil, errors.New("config: YIBAN_HASH_PEPPER is required (at least 16 characters)")
	}
	cfg.HashPepper = []byte(rawPepper)
	return cfg, nil
}

func get(src Source, key, fallback string) string {
	if v, ok := src.Lookup(key); ok && v != "" {
		return v
	}
	return fallback
}
