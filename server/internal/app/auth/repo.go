// Package auth 声明会话与密码重置的仓储端口。
//
// # 功能
// 声明会话仓储与一次性重置令牌仓储，并转发 core/auth 的类型。
// 本包**不**签发令牌、**不**派生 CSRF、**不**写 SQL。
//
// # 归属
// app 层用例端口。行类型留在 core/auth，
// 这样 PostgreSQL 适配器无需导入本包（canonical.tsv 的 boundary.adapters）。
//
// # 复用
// SessionRepo、PasswordResetRepo；类型别名 Session、NewSession、
// ResolvedSession、PasswordReset、NewPasswordReset。
//
// # 口径
// 解析只返回有效会话；未知、已撤销、绝对过期、空闲超时一律 not_found。
// 每用户有效会话上限取自 core/auth.MaxSessionsPerUser。
package auth

import (
	"context"
	"time"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

// Session 是一行 sessions 记录。
type Session = coreauth.SessionRow

// NewSession 是创建会话的输入。
type NewSession = coreauth.NewSessionRow

// ResolvedSession 是有效会话与其用户的连接结果。
type ResolvedSession = coreauth.ResolvedSession

// PasswordReset 是一行 password_resets 记录。
type PasswordReset = coreauth.PasswordReset

// NewPasswordReset 是创建重置令牌的输入。
type NewPasswordReset = coreauth.NewPasswordReset

// SessionRepo 是会话仓储端口。
//
// Resolve 只返回有效会话：未知、已撤销、绝对过期、空闲过期
// 都产出 not_found（errs.CodeNotFound）。解析成功会刷新空闲窗口。
// Create 每用户最多保留 coreauth.MaxSessionsPerUser 个有效会话，
// 超出即淘汰最旧的一个。
type SessionRepo interface {
	Create(ctx context.Context, in NewSession) (Session, error)
	Resolve(ctx context.Context, tokenHash coreauth.TokenHash, now time.Time, idle time.Duration) (ResolvedSession, error)
	Revoke(ctx context.Context, tokenHash coreauth.TokenHash, now time.Time) error
	RevokeAllForUser(ctx context.Context, userID string, now time.Time) (int64, error)
	DeleteExpired(ctx context.Context, before time.Time, limit int) (int64, error)
}

// PasswordResetRepo 是一次性重置令牌仓储端口。
//
// Consume 标记该行已用并返回它。
// **第二次 Consume、过期令牌、未知令牌一律 not_found**。
type PasswordResetRepo interface {
	Create(ctx context.Context, in NewPasswordReset) (PasswordReset, error)
	Consume(ctx context.Context, tokenHash coreauth.TokenHash, now time.Time) (PasswordReset, error)
}
