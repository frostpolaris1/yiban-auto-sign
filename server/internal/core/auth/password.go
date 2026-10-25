package auth

// 口令哈希的算法与参数。本文件是口令格式的唯一解释点。
//
// # 算法溯源
// 新口令用 argon2id（RFC 9106），实现在 golang.org/x/crypto/argon2。
// 旧口令用 werkzeug 的 scrypt 串，格式 `scrypt:N:r:p$salt$hash`。
//
// # 功能
// 生成 argon2id PHC 串，校验新旧两种格式，并报告是否需要重哈希。
// 本文件**不**读写数据库，**不**决定何时重哈希。
//
// # 单位
// memory 的单位是 KiB，time 是迭代次数，threads 是并行度。
// 参数一并写进 PHC 串，故校验按串内参数执行，不按当前常量。
//
// # 口径
// 新哈希参数固定为 m=19456 KiB、t=2、p=1、盐 16 字节、摘要 32 字节。
// **参数变动必须改常量**：校验读串内参数，重哈希信号据此产生。

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"strconv"
	"strings"

	"golang.org/x/crypto/argon2"
	"golang.org/x/crypto/scrypt"
)

// argon2id 参数。取 OWASP 口令存储建议的第二档（19 MiB、2 次迭代、单线程）。
// 参数写入 PHC 串，便于逐行判定重哈希。
const (
	argonMemory  uint32 = 19 * 1024
	argonTime    uint32 = 2
	argonThreads uint8  = 1
	argonSaltLen        = 16
	argonKeyLen  uint32 = 32
)

// 校验侧上界。库内的哈希串按可信数据对待，
// 但坏行不得让进程分配无界内存。
//
// 上界取现用参数的 2 倍，故单次校验最多付当前登录成本的内存与迭代各 2 倍。
// argon2 现用 m=19456、t=2、p=1，上界据此取 38912、4、2。
// scrypt 实测最大 N*r = 65536*8 = 524288，上界取 1048576（约 128 MiB）。
// 上界防的是**数据事故**，不是用户输入：哈希串只来自库内。
const (
	maxArgonMemory  uint32 = 2 * argonMemory
	maxArgonTime    uint32 = 2 * argonTime
	maxArgonThreads uint8  = 2 * argonThreads
	maxScryptN             = 1 << 20
	maxScryptRP            = 32
	// scrypt 内存约 128*N*r 字节。N*r 上限 1M 把单次分配压到 128 MiB。
	maxScryptWork = 1 << 20
	maxDigestLen  = 128
)

// ErrHashFormat 表示哈希串不是已知格式，或参数越界。
// 它与「口令错误」不同：调用方据此区分坏数据与坏凭据。
var ErrHashFormat = errors.New("auth: password hash has an unknown or invalid format")

// PasswordCheck 是校验结果。
// **NeedsRehash 只表示格式或参数过期**，不表示口令错误。
type PasswordCheck struct {
	Valid       bool
	NeedsRehash bool
}

// HashPassword 返回 password 的 argon2id PHC 串。
//
// 每次调用重新生成 16 字节盐，故同一口令得到不同串。
// 空口令是错误：调用方必须先做输入校验。
func HashPassword(password string) (string, error) {
	if password == "" {
		return "", errors.New("auth: empty password")
	}
	salt := make([]byte, argonSaltLen)
	if _, err := rand.Read(salt); err != nil {
		return "", fmt.Errorf("auth: read salt: %w", err)
	}
	digest := argon2.IDKey([]byte(password), salt, argonTime, argonMemory, argonThreads, argonKeyLen)
	return fmt.Sprintf("$argon2id$v=%d$m=%d,t=%d,p=%d$%s$%s",
		argon2.Version, argonMemory, argonTime, argonThreads,
		base64.RawStdEncoding.EncodeToString(salt),
		base64.RawStdEncoding.EncodeToString(digest)), nil
}

// VerifyPassword 校验 password 与 encoded 是否匹配。
//
// 支持两代格式：argon2id PHC 串与 werkzeug scrypt 串。
// 旧格式与参数过期的 argon2id 串都回报 NeedsRehash。
// 未知格式或越界参数返回 ErrHashFormat，**不静默当作口令错误**。
func VerifyPassword(password, encoded string) (PasswordCheck, error) {
	switch {
	case strings.HasPrefix(encoded, "$argon2id$"):
		return verifyArgon2id(password, encoded)
	case strings.HasPrefix(encoded, "scrypt:"):
		return verifyScrypt(password, encoded)
	default:
		return PasswordCheck{}, fmt.Errorf("%w: unknown algorithm", ErrHashFormat)
	}
}

func verifyArgon2id(password, encoded string) (PasswordCheck, error) {
	parts := strings.Split(encoded, "$")
	// 形如 ["", "argon2id", "v=19", "m=…,t=…,p=…", salt, hash]。
	if len(parts) != 6 || parts[1] != "argon2id" {
		return PasswordCheck{}, fmt.Errorf("%w: argon2id layout", ErrHashFormat)
	}
	version, err := parsePrefixedInt(parts[2], "v=")
	if err != nil {
		return PasswordCheck{}, err
	}
	params := map[string]uint32{}
	for _, item := range strings.Split(parts[3], ",") {
		key, value, ok := strings.Cut(item, "=")
		if !ok {
			return PasswordCheck{}, fmt.Errorf("%w: argon2id parameter %q", ErrHashFormat, item)
		}
		parsed, err := strconv.ParseUint(value, 10, 32)
		if err != nil {
			return PasswordCheck{}, fmt.Errorf("%w: argon2id parameter %q", ErrHashFormat, item)
		}
		params[key] = uint32(parsed)
	}
	timeCost, hasTime := params["t"]
	threadCount, hasThreads := params["p"]
	memoryCost, hasMemory := params["m"]
	if !hasTime || !hasThreads || !hasMemory {
		return PasswordCheck{}, fmt.Errorf("%w: argon2id parameters are incomplete", ErrHashFormat)
	}
	if memoryCost == 0 || memoryCost > maxArgonMemory ||
		timeCost == 0 || timeCost > maxArgonTime ||
		threadCount == 0 || threadCount > uint32(maxArgonThreads) {
		return PasswordCheck{}, fmt.Errorf("%w: argon2id parameters are out of range", ErrHashFormat)
	}
	salt, err := decodeBase64(parts[4])
	if err != nil {
		return PasswordCheck{}, fmt.Errorf("%w: argon2id salt", ErrHashFormat)
	}
	want, err := decodeBase64(parts[5])
	if err != nil {
		return PasswordCheck{}, fmt.Errorf("%w: argon2id digest", ErrHashFormat)
	}
	if len(want) == 0 || len(want) > maxDigestLen {
		return PasswordCheck{}, fmt.Errorf("%w: argon2id digest length", ErrHashFormat)
	}
	got := argon2.IDKey([]byte(password), salt, timeCost, memoryCost, uint8(threadCount), uint32(len(want)))
	valid := subtle.ConstantTimeCompare(got, want) == 1
	needsRehash := uint32(version) != argon2.Version ||
		memoryCost != argonMemory || timeCost != argonTime ||
		threadCount != uint32(argonThreads) ||
		len(salt) != argonSaltLen || len(want) != int(argonKeyLen)
	return PasswordCheck{Valid: valid, NeedsRehash: needsRehash}, nil
}

func verifyScrypt(password, encoded string) (PasswordCheck, error) {
	parts := strings.Split(encoded, "$")
	if len(parts) != 3 {
		return PasswordCheck{}, fmt.Errorf("%w: scrypt layout", ErrHashFormat)
	}
	method := strings.Split(parts[0], ":")
	if len(method) != 4 || method[0] != "scrypt" {
		return PasswordCheck{}, fmt.Errorf("%w: scrypt parameters", ErrHashFormat)
	}
	n, err := strconv.Atoi(method[1])
	if err != nil || n < 2 || n > maxScryptN || n&(n-1) != 0 {
		return PasswordCheck{}, fmt.Errorf("%w: scrypt N", ErrHashFormat)
	}
	r, err := strconv.Atoi(method[2])
	if err != nil || r < 1 || r > maxScryptRP {
		return PasswordCheck{}, fmt.Errorf("%w: scrypt r", ErrHashFormat)
	}
	p, err := strconv.Atoi(method[3])
	if err != nil || p < 1 || p > maxScryptRP {
		return PasswordCheck{}, fmt.Errorf("%w: scrypt p", ErrHashFormat)
	}
	if int64(n)*int64(r) > maxScryptWork {
		return PasswordCheck{}, fmt.Errorf("%w: scrypt work factor", ErrHashFormat)
	}
	want, err := hex.DecodeString(parts[2])
	if err != nil || len(want) == 0 || len(want) > maxDigestLen {
		return PasswordCheck{}, fmt.Errorf("%w: scrypt digest", ErrHashFormat)
	}
	got, err := scrypt.Key([]byte(password), []byte(parts[1]), n, r, p, len(want))
	if err != nil {
		return PasswordCheck{}, fmt.Errorf("%w: scrypt key: %v", ErrHashFormat, err)
	}
	valid := subtle.ConstantTimeCompare(got, want) == 1
	// 旧格式一律重哈希；参数由当前 argon2id 常量取代。
	//
	// 计时差已登记豁免（裁决 P2-2，2026-10-26）。
	// 本机实测口径（localhost 真库，40 轮中位数）：旧 scrypt 98 ms，argon2id 21 ms。
	// 人群仅限 P8 导入后未登录过的用户；登录成功即重哈希，窗口随之关闭。
	// 拉平要给全部 argon2 用户垫到 98 ms，每次登录多付约 5 倍 CPU，故否决。
	return PasswordCheck{Valid: valid, NeedsRehash: true}, nil
}

func parsePrefixedInt(value, prefix string) (int, error) {
	if !strings.HasPrefix(value, prefix) {
		return 0, fmt.Errorf("%w: missing %s", ErrHashFormat, prefix)
	}
	parsed, err := strconv.Atoi(strings.TrimPrefix(value, prefix))
	if err != nil {
		return 0, fmt.Errorf("%w: %s", ErrHashFormat, prefix)
	}
	return parsed, nil
}

// decodeBase64 接受无填充与标准填充两种写法。
// PHC 标准要求无填充；带填充的旧行也应可读。
func decodeBase64(value string) ([]byte, error) {
	if decoded, err := base64.RawStdEncoding.DecodeString(value); err == nil {
		return decoded, nil
	}
	return base64.StdEncoding.DecodeString(value)
}
