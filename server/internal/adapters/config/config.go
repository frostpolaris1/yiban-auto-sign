// Package config 是读取环境变量的唯一位置。
//
// # 功能
// 读取、校验并定型启动配置：环境名、监听地址、数据库 DSN、时区、
// 不安全 cookie 开关、哈希 pepper、凭据加密钥。
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
// YIBAN_CREDENTIAL_KEY 是 32 字节的 base64，形状错即启动错误。
package config

import (
	"encoding/base64"
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	corerate "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/rate"
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
//
// 三把密钥用途不同，轮换后果也不同：
//   - YIBAN_HASH_PEPPER 派生 IP 与 User-Agent 摘要，并派生审计链钥。
//     轮换即审计链断：旧行无法再校验，需要一次迁移口径。
//   - YIBAN_CREDENTIAL_KEY 直接用于凭据的 AES-256-GCM。
//     轮换即走 kid 重加密（P4/P8），旧行仍可解密。
//     凭据钥**不**从 pepper 派生：否则轮换 pepper 就是数据丢失。
type Config struct {
	Env             string
	HTTPAddr        string
	DatabaseURL     string
	Timezone        string
	InsecureCookies bool
	// HashPepper 是 IP 与 User-Agent 摘要的 HMAC 密钥，
	// 也是审计链钥的派生源。
	// **必填**：缺少它的部署会静默丢掉滥用线索。
	HashPepper []byte
	// CredentialKey 是凭据加密钥，解码后 32 字节。
	// **必填**：缺少它的部署无法加密新凭据。
	CredentialKey []byte

	// 以下为调度面参数。缺省值见 config/registry.json；
	// 越界即启动错误，不静默夹取。
	Workers          int
	AvgAttempt       time.Duration
	EgressRate       float64
	EdgeFront        time.Duration
	EdgeBack         time.Duration
	AllowTimePref    bool
	TimePrefCooldown time.Duration
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

	// 凭据钥是独立配置项，不从 pepper 派生。
	// 只用标准 base64：两种编码都接受会让同一文本有两种解码结果。
	rawKey, keySet := src.Lookup("YIBAN_CREDENTIAL_KEY")
	if !keySet {
		return nil, errors.New("config: YIBAN_CREDENTIAL_KEY is required (base64 of 32 bytes)")
	}
	key, err := base64.StdEncoding.DecodeString(strings.TrimSpace(rawKey))
	if err != nil {
		return nil, errors.New("config: YIBAN_CREDENTIAL_KEY must be valid base64")
	}
	if len(key) != 32 {
		return nil, fmt.Errorf("config: YIBAN_CREDENTIAL_KEY must decode to 32 bytes, got %d", len(key))
	}
	cfg.CredentialKey = key

	// 调度参数：名册登记的域名在启动时校验。
	// 非法值必须失败：静默夹取会让容量与限速口径悄悄改变。
	if cfg.Workers, err = getInt(src, "YIBAN_WORKERS", 1, 1, 64); err != nil {
		return nil, err
	}
	avg, err := getInt(src, "YIBAN_AVG_ATTEMPT_SEC", 3, 1, 300)
	if err != nil {
		return nil, err
	}
	cfg.AvgAttempt = time.Duration(avg) * time.Second
	if cfg.EgressRate, err = getFloat(src, "YIBAN_EGRESS_RATE", corerate.DefaultRate, 0.2, 4.0); err != nil {
		return nil, err
	}
	edgeFront, err := getInt(src, "YIBAN_WINDOW_EDGE_FRONT_SEC", 60, 0, 300)
	if err != nil {
		return nil, err
	}
	cfg.EdgeFront = time.Duration(edgeFront) * time.Second
	edgeBack, err := getInt(src, "YIBAN_WINDOW_EDGE_BACK_SEC", 60, 0, 300)
	if err != nil {
		return nil, err
	}
	cfg.EdgeBack = time.Duration(edgeBack) * time.Second
	if cfg.AllowTimePref, err = getBool(src, "YIBAN_ALLOW_TIME_PREF", false); err != nil {
		return nil, err
	}
	cooldown, err := getInt(src, "YIBAN_TIME_PREF_COOLDOWN_SEC", 30, 0, 3600)
	if err != nil {
		return nil, err
	}
	cfg.TimePrefCooldown = time.Duration(cooldown) * time.Second
	return cfg, nil
}

// getInt 读取整数配置并在域名内校验。
func getInt(src Source, key string, fallback, min, max int) (int, error) {
	raw, ok := src.Lookup(key)
	if !ok || strings.TrimSpace(raw) == "" {
		return fallback, nil
	}
	v, err := strconv.Atoi(strings.TrimSpace(raw))
	if err != nil {
		return 0, fmt.Errorf("config: %s must be an integer", key)
	}
	if v < min || v > max {
		return 0, fmt.Errorf("config: %s must be between %d and %d", key, min, max)
	}
	return v, nil
}

// getFloat 读取浮点配置并在域名内校验。
func getFloat(src Source, key string, fallback, min, max float64) (float64, error) {
	raw, ok := src.Lookup(key)
	if !ok || strings.TrimSpace(raw) == "" {
		return fallback, nil
	}
	v, err := strconv.ParseFloat(strings.TrimSpace(raw), 64)
	if err != nil {
		return 0, fmt.Errorf("config: %s must be a number", key)
	}
	if v < min || v > max {
		return 0, fmt.Errorf("config: %s must be between %g and %g", key, min, max)
	}
	return v, nil
}

// getBool 读取布尔配置。
func getBool(src Source, key string, fallback bool) (bool, error) {
	raw, ok := src.Lookup(key)
	if !ok || strings.TrimSpace(raw) == "" {
		return fallback, nil
	}
	v, err := strconv.ParseBool(strings.TrimSpace(raw))
	if err != nil {
		return false, fmt.Errorf("config: %s must be a boolean", key)
	}
	return v, nil
}

func get(src Source, key, fallback string) string {
	if v, ok := src.Lookup(key); ok && v != "" {
		return v
	}
	return fallback
}
