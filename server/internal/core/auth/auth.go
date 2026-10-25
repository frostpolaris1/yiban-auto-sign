// Package auth 定义 HTTP 入口与会话存储之间的会话契约。
//
// # 功能
// 定义会话、令牌摘要、CSRF 派生、角色与用户状态词表。
// 本包**不**实现存储，**不**读写数据库，**不**做密码校验。
// 存储实现见 adapters/session 与 adapters/pg。
//
// # 归属
// core 层纯领域包。entry/http 读会话，app/auth 声明仓储端口，
// adapters 实现 SessionIssuer 与 SessionRepo。
//
// # 复用
// CookieName、InsecureCookieName、MaxSessionsPerUser；
// TokenHash 及 HashToken、Bytes、IsZero；
// IPHash、UserAgentHash 及 HashIP、HashUserAgent、HashValue、FromStored 系列；
// CSRFToken；Role、Status；User、Session、Issued；
// SessionRow、NewSessionRow、ResolvedSession、PasswordReset、NewPasswordReset；
// SessionIssuer 接口与 WithSession、FromContext。
//
// # 口径
// 会话令牌是 32 字节随机值，经 HttpOnly cookie 下发。
// 存储侧**只存 SHA-256 摘要**，原始令牌不离开客户端。
// CSRF 令牌由会话令牌与服务器密钥 HMAC 派生，**不落盘**。
// IP 与 User-Agent 只存 HMAC 摘要，pepper 为空即报错。
package auth

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"regexp"
	"time"
)

// CookieName 是会话 cookie 名。
// __Host- 前缀要求同时满足 Secure、Path=/、无 Domain 三项。
const CookieName = "__Host-yiban_session"

// InsecureCookieName 仅用于进程位于 TLS 终结器之后、无法设置 Secure cookie 的场景。
// 浏览器会拒绝不带 Secure 的 __Host- cookie，故开发名**必须**去掉前缀。
const InsecureCookieName = "yiban_session_dev"

// MaxSessionsPerUser 限制每用户同时有效会话数 20。
// 新登录淘汰最旧会话，**不无界增长**（决策 12.9）。
const MaxSessionsPerUser = 20

// TokenHash 是会话令牌或重置令牌的 SHA-256 摘要。
//
// **数据库只存该摘要**。独立类型的作用是编译期拦截：
// []byte 与 string 不能直接传入 token_hash 列。请用 HashToken 构造。
type TokenHash [32]byte

// HashToken 返回 raw 的 SHA-256 摘要。
// 这是调用方唯一需要的构造函数；**原始令牌不存储**。
func HashToken(raw string) TokenHash { return TokenHash(sha256.Sum256([]byte(raw))) }

// Bytes 返回摘要副本供数据库驱动使用。
// 返回副本使数组在调用后保持不可变。
func (h TokenHash) Bytes() []byte {
	out := make([]byte, len(h))
	copy(out, h[:])
	return out
}

// IsZero 报告摘要是否全零。全零表示调用方未设置该字段。
func (h TokenHash) IsZero() bool { return h == TokenHash{} }

// IPHash 是 IP 地址的带密钥摘要。
// digest 字段不导出，原始地址无法转成该类型：
// HashIP 是唯一构造函数，且空 pepper 直接报错，不存无密钥哈希。
type IPHash struct {
	digest string
}

// UserAgentHash 是 User-Agent 字符串的带密钥摘要。
type UserAgentHash struct {
	digest string
}

// HashValue 返回 value 在 pepper 下的 HMAC-SHA256 小写十六进制。
//
// 裸 SHA-256 会让任何人枚举 IPv4 空间反推地址。
// **空 pepper 是错误，不是静默默认**：缺少 pepper 的部署必须在启动时失败，
// 而不是丢掉滥用线索。
func HashValue(pepper []byte, value string) (string, error) {
	if len(pepper) == 0 {
		return "", errors.New("auth: empty hash pepper")
	}
	mac := hmac.New(sha256.New, pepper)
	mac.Write([]byte(value))
	return hex.EncodeToString(mac.Sum(nil)), nil
}

// HashIP 用服务器 pepper 哈希一个地址。
func HashIP(pepper []byte, ip string) (IPHash, error) {
	d, err := HashValue(pepper, ip)
	if err != nil {
		return IPHash{}, err
	}
	return IPHash{digest: d}, nil
}

// HashUserAgent 用服务器 pepper 哈希 User-Agent 字符串。
func HashUserAgent(pepper []byte, ua string) (UserAgentHash, error) {
	d, err := HashValue(pepper, ua)
	if err != nil {
		return UserAgentHash{}, err
	}
	return UserAgentHash{digest: d}, nil
}

// String 返回可存储的摘要。
func (h IPHash) String() string { return h.digest }

// String 返回可存储的摘要。
func (h UserAgentHash) String() string { return h.digest }

// IsZero 报告是否未计算摘要。
func (h IPHash) IsZero() bool { return h.digest == "" }

// IsZero 报告是否未计算摘要。
func (h UserAgentHash) IsZero() bool { return h.digest == "" }

// storedHashPattern 是摘要的存储形式：64 位小写十六进制。
var storedHashPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

// IPHashFromStored 包装从数据库读回的摘要。
// 空值即存储的 NULL，返回零值；非空值必须是带密钥形式，
// 防止其他路径写入的原始值冒充摘要。
func IPHashFromStored(hexDigest string) (IPHash, error) {
	if hexDigest == "" {
		return IPHash{}, nil
	}
	if !storedHashPattern.MatchString(hexDigest) {
		return IPHash{}, errors.New("auth: stored hash has the wrong form")
	}
	return IPHash{digest: hexDigest}, nil
}

// UserAgentHashFromStored 包装从数据库读回的摘要。
// 空值即存储的 NULL，返回零值。
func UserAgentHashFromStored(hexDigest string) (UserAgentHash, error) {
	if hexDigest == "" {
		return UserAgentHash{}, nil
	}
	if !storedHashPattern.MatchString(hexDigest) {
		return UserAgentHash{}, errors.New("auth: stored hash has the wrong form")
	}
	return UserAgentHash{digest: hexDigest}, nil
}

// CSRFToken 由会话令牌与服务器密钥派生 CSRF 令牌。
// **不存储任何值**：每个请求重新计算，数据库泄露也拿不到 CSRF 令牌。
func CSRFToken(secret []byte, sessionToken string) string {
	mac := hmac.New(sha256.New, secret)
	mac.Write([]byte("csrf:"))
	mac.Write([]byte(sessionToken))
	return base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

// Role 是用户的授权角色。
type Role string

// 角色集合，v1 冻结。
const (
	RoleMaster Role = "master"
	RoleAdmin  Role = "admin"
	RoleUser   Role = "user"
)

// Valid 报告 r 是否为已知角色。
func (r Role) Valid() bool { return r == RoleMaster || r == RoleAdmin || r == RoleUser }

// Status 是用户的账号状态。
type Status string

// 用户状态集合。**disabled 用户不能登录**。
const (
	StatusActive   Status = "active"
	StatusDisabled Status = "disabled"
)

// Valid 报告 s 是否为已知用户状态。
func (s Status) Valid() bool { return s == StatusActive || s == StatusDisabled }

// User 是已认证主体。
type User struct {
	ID                 string
	Email              string
	Role               Role
	Status             Status
	MustChangePassword bool
}

// Session 是挂在请求上的服务端会话状态。
type Session struct {
	User      User
	CSRFToken string
}

// Issued 是新建会话及其原始令牌。
// 令牌只向调用方返回一次，**从不以明文形式存储**。
type Issued struct {
	Token string
	Session
}

// SessionRow 是一行 sessions 记录。
// 存储侧只保留令牌摘要这一种凭据形式。
// IPHash 与 UserAgentHash 承载 HashIP、HashUserAgent 的输出，
// **绝不是原始地址或原始 UA 串**。
type SessionRow struct {
	ID            string
	UserID        string
	TokenHash     TokenHash
	AuthMethod    string
	CreatedAt     time.Time
	LastSeenAt    time.Time
	IdleExpiresAt time.Time
	ExpiresAt     time.Time
	RevokedAt     *time.Time
	IPHash        IPHash
	UserAgentHash UserAgentHash
}

// NewSessionRow 是创建会话的输入。
// TokenHash 必须来自 HashToken；
// IPHash 与 UserAgentHash 必须来自 HashIP 与 HashUserAgent。
// 仓储原样存储，**不再哈希**。
type NewSessionRow struct {
	UserID        string
	TokenHash     TokenHash
	AuthMethod    string
	Now           time.Time
	IdleExpiresAt time.Time
	ExpiresAt     time.Time
	IPHash        IPHash
	UserAgentHash UserAgentHash
}

// ResolvedSession 是有效会话与其用户的连接结果。
type ResolvedSession struct {
	Session SessionRow
	User    User
}

// PasswordReset 是一个一次性密码重置令牌。
type PasswordReset struct {
	ID        string
	UserID    string
	TokenHash TokenHash
	CreatedAt time.Time
	ExpiresAt time.Time
	UsedAt    *time.Time
}

// NewPasswordReset 是创建重置令牌的输入。
// TokenHash 必须来自 HashToken。
type NewPasswordReset struct {
	UserID    string
	TokenHash TokenHash
	Now       time.Time
	ExpiresAt time.Time
}

// ErrNoSession 表示会话缺失、过期或已撤销。
var ErrNoSession = errors.New("auth: no session")

// SessionIssuer 创建、解析与撤销会话。
//
// Issue **总是签发新令牌**：登录前的会话不复用。
// Resolve 对未知、过期、已撤销令牌给出同一结果 ErrNoSession。
// Revoke 与 RevokeAllForUser 幂等。
type SessionIssuer interface {
	Issue(ctx context.Context, user User) (Issued, error)
	Resolve(ctx context.Context, token string) (Session, error)
	Revoke(ctx context.Context, token string) error
	RevokeAllForUser(ctx context.Context, userID string) error
}

type contextKey struct{}

var sessionKey contextKey

// WithSession 返回携带 sess 的 context。
func WithSession(ctx context.Context, sess Session) context.Context {
	return context.WithValue(ctx, sessionKey, sess)
}

// FromContext 返回 ctx 携带的会话。第二个返回值报告是否存在。
func FromContext(ctx context.Context) (Session, bool) {
	sess, ok := ctx.Value(sessionKey).(Session)
	return sess, ok
}
