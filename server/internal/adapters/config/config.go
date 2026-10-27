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
	"sort"
	"strconv"
	"strings"
	"time"

	corerate "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/rate"
)

// Source 是环境抽象。测试传入 map 实现。
type Source interface {
	Lookup(key string) (string, bool)
}

// EnumeratedSource 是能枚举全部键值对的 Source。
// 进程环境天然支持；doctor 的「未登记键」体检需要枚举面。
type EnumeratedSource interface {
	Source
	// Environ 返回全部键值对，形状同 os.Environ()（KEY=VALUE）。
	Environ() []string
}

// Env 读取进程环境。
type Env struct{}

// Lookup 实现 Source。
func (Env) Lookup(key string) (string, bool) { return os.LookupEnv(key) }

// Environ 实现 EnumeratedSource。
func (Env) Environ() []string { return os.Environ() }

// PrefixedKeys 返回 src 里以 prefix 开头的键名，**不含值**。
// doctor 的「未登记键」体检用它枚举 YIBAN_*；枚举语义只住在 config，
// 枚举结果永不携带值，敏感值不出本包。
func PrefixedKeys(src EnumeratedSource, prefix string) []string {
	var keys []string
	for _, kv := range src.Environ() {
		name, _, ok := strings.Cut(kv, "=")
		if ok && strings.HasPrefix(name, prefix) {
			keys = append(keys, name)
		}
	}
	sort.Strings(keys)
	return keys
}

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

	// 以下为调度面参数。缺省值与域见 config/registry.json；
	// 两侧相等由 registry_parity_test 钉住。越界即启动错误，不静默夹取。
	Workers          int
	AvgAttempt       time.Duration
	EgressRate       float64
	EdgeFront        time.Duration
	EdgeBack         time.Duration
	AllowTimePref    bool
	TimePrefCooldown time.Duration

	// PhotoDir 是图片 blob 存储根目录。
	PhotoDir string
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

// DefaultTimezone 是 YIBAN_TZ 未设置时的缺省时区。
// doctor 的时区体检与 LoadFrom 共用本常量，口径只有一份。
const DefaultTimezone = "Asia/Shanghai"

// DefaultHTTPAddr 是 YIBAN_HTTP_ADDR 未设置时的缺省监听地址。
// 只绑回环：暴露需显式改。doctor 的端口体检与 healthcheck 子命令共用本常量。
const DefaultHTTPAddr = "127.0.0.1:47183"

// DefaultPhotoDir 是 YIBAN_PHOTO_DIR 未设置时的缺省存储根。
// doctor 的文件系统面体检共用本常量。
const DefaultPhotoDir = "data/photos"

// keySpec 是名册对照测试看到的读取规格视图。
// domain 为 nil 表示名册不登记域；fallback 的类型就是键的类型。
type keySpec struct {
	key      string
	fallback any
	domain   []float64
}

// intKey 是整数配置键的读取规格。
type intKey struct {
	key      string
	fallback int
	min, max int
}

func (k intKey) read(src Source) (int, error) {
	return getInt(src, k.key, k.fallback, k.min, k.max)
}

func (k intKey) spec() keySpec {
	return keySpec{
		key:      k.key,
		fallback: k.fallback,
		domain:   []float64{float64(k.min), float64(k.max)},
	}
}

// floatKey 是浮点配置键的读取规格。
type floatKey struct {
	key      string
	fallback float64
	min, max float64
}

func (k floatKey) read(src Source) (float64, error) {
	return getFloat(src, k.key, k.fallback, k.min, k.max)
}

func (k floatKey) spec() keySpec {
	return keySpec{
		key:      k.key,
		fallback: k.fallback,
		domain:   []float64{k.min, k.max},
	}
}

// boolKey 是布尔配置键的读取规格。
type boolKey struct {
	key      string
	fallback bool
}

func (k boolKey) read(src Source) (bool, error) {
	return getBool(src, k.key, k.fallback)
}

func (k boolKey) spec() keySpec {
	return keySpec{key: k.key, fallback: k.fallback}
}

// stringKey 是字符串配置键的读取规格。
type stringKey struct {
	key      string
	fallback string
}

func (k stringKey) read(src Source) string {
	return get(src, k.key, k.fallback)
}

func (k stringKey) spec() keySpec {
	return keySpec{key: k.key, fallback: k.fallback}
}

// 读取规格表：缺省与域在代码里只出现一次。
// LoadFrom 逐键取规格读取；registry_parity_test 用同一张表对 config/registry.json。
// 浮点键的域取 core/rate 的常量，不在本包复写 0.2/4.0。
var (
	keyWorkers          = intKey{"YIBAN_WORKERS", 1, 1, 64}
	keyAvgAttemptSec    = intKey{"YIBAN_AVG_ATTEMPT_SEC", 3, 1, 300}
	keyWindowEdgeFront  = intKey{"YIBAN_WINDOW_EDGE_FRONT_SEC", 60, 0, 300}
	keyWindowEdgeBack   = intKey{"YIBAN_WINDOW_EDGE_BACK_SEC", 60, 0, 300}
	keyTimePrefCooldown = intKey{"YIBAN_TIME_PREF_COOLDOWN_SEC", 30, 0, 3600}
	keyEgressRate       = floatKey{"YIBAN_EGRESS_RATE", corerate.DefaultRate, corerate.RateMin, corerate.RateMax}
	keyAllowTimePref    = boolKey{"YIBAN_ALLOW_TIME_PREF", false}
	keyHTTPAddr         = stringKey{"YIBAN_HTTP_ADDR", DefaultHTTPAddr}
	keyTimezone         = stringKey{"YIBAN_TZ", DefaultTimezone}
	keyPhotoDir         = stringKey{"YIBAN_PHOTO_DIR", DefaultPhotoDir}
	keyDatabaseURL      = stringKey{"YIBAN_DATABASE_URL", ""}
)

// configSpecs 汇总静态缺省/域的读取规格。
// 新增读取点必须同批进本表：TestEveryConfigReadHasRegistryComparison 扫 config.go
// 的键字面量，表外出现即红。
func configSpecs() []keySpec {
	return []keySpec{
		keyWorkers.spec(),
		keyAvgAttemptSec.spec(),
		keyWindowEdgeFront.spec(),
		keyWindowEdgeBack.spec(),
		keyTimePrefCooldown.spec(),
		keyEgressRate.spec(),
		keyAllowTimePref.spec(),
		keyHTTPAddr.spec(),
		keyTimezone.spec(),
		keyPhotoDir.spec(),
		keyDatabaseURL.spec(),
	}
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
		HTTPAddr:    keyHTTPAddr.read(src),
		DatabaseURL: strings.TrimSpace(keyDatabaseURL.read(src)),
		Timezone:    keyTimezone.read(src),
		// 本地 HTTP 无法设置 Secure cookie，
		// 故无前缀名只在 development 与 test 下作为默认。
		InsecureCookies: env == "development" || env == "test",
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
	if cfg.Workers, err = keyWorkers.read(src); err != nil {
		return nil, err
	}
	avg, err := keyAvgAttemptSec.read(src)
	if err != nil {
		return nil, err
	}
	cfg.AvgAttempt = time.Duration(avg) * time.Second
	if cfg.EgressRate, err = keyEgressRate.read(src); err != nil {
		return nil, err
	}
	edgeFront, err := keyWindowEdgeFront.read(src)
	if err != nil {
		return nil, err
	}
	cfg.EdgeFront = time.Duration(edgeFront) * time.Second
	edgeBack, err := keyWindowEdgeBack.read(src)
	if err != nil {
		return nil, err
	}
	cfg.EdgeBack = time.Duration(edgeBack) * time.Second
	if cfg.AllowTimePref, err = keyAllowTimePref.read(src); err != nil {
		return nil, err
	}
	cooldown, err := keyTimePrefCooldown.read(src)
	if err != nil {
		return nil, err
	}
	cfg.TimePrefCooldown = time.Duration(cooldown) * time.Second

	cfg.PhotoDir = strings.TrimSpace(keyPhotoDir.read(src))
	if cfg.PhotoDir == "" {
		return nil, errors.New("config: YIBAN_PHOTO_DIR must not be empty")
	}
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
