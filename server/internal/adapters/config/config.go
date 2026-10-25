// Package config 是读取环境变量的唯一位置。
//
// # 功能
// 读取、校验并定型启动配置：环境名、监听地址、数据库 DSN、时区、
// 不安全 cookie 开关、哈希 pepper。
// 本包**不**连库、**不**启动服务、**不**读命令行参数。
//
// # 归属
// adapters 层设施包。cmd/server 调 Load；
// 测试用 LoadFrom 注入 map，不改进程环境。
//
// # 复用
// Source、Env、Config、Load、LoadFrom。
//
// # 口径
// 环境名是闭集，未知值即启动错误。
// YIBAN_HASH_PEPPER 至少 16 字符；production 拒绝不安全 cookie。
package config

import (
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"
)

// Source 是环境抽象。测试传入 map 实现。
type Source interface {
	Lookup(key string) (string, bool)
}

// Env 读取进程环境。
type Env struct{}

// Lookup 实现 Source。
func (Env) Lookup(key string) (string, bool) { return os.LookupEnv(key) }

// Config 是定型后的运行时配置。
type Config struct {
	Env             string
	HTTPAddr        string
	DatabaseURL     string
	Timezone        string
	InsecureCookies bool
	// HashPepper 是 IP 与 User-Agent 摘要的 HMAC 密钥。
	// **必填**：缺少它的部署会静默丢掉滥用线索。
	HashPepper []byte
}

// knownEnvs 是闭集。
// 未知值即启动错误：像 "prod" 这样的笔误，
// **不得静默选中开发用 cookie 名**。
var knownEnvs = map[string]bool{
	"development": true,
	"test":        true,
	"staging":     true,
	"production":  true,
}

// Load 读取进程环境。
func Load() (*Config, error) { return LoadFrom(Env{}) }

// LoadFrom 从 src 读取。它**从不**自行读进程环境。
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
		// 本地 HTTP 无法设置 Secure cookie，
		// 故无前缀名只在 development 与 test 下作为默认。
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
	cfg.HashPepper = []byte(strings.TrimSpace(rawPepper))
	return cfg, nil
}

func get(src Source, key, fallback string) string {
	if v, ok := src.Lookup(key); ok && v != "" {
		return v
	}
	return fallback
}
