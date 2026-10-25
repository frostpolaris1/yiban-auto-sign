package pg

// DB 版 SessionIssuer。它把会话仓储与审计链组合成 core/auth 的签发端口。
//
// # 功能
// 签发、解析、撤销会话；签发时在同一事务内写入 auth.login 审计行。
// 本文件**不**校验口令，**不**决定会话时长之外的政策。
//
// # 归属
// adapters 层。cmd/server 用它替换内存版；
// 用例层只依赖 core/auth.SessionIssuer，不见本类型。
//
// # 口径
// 令牌 32 字节随机，库内只存 SHA-256。
// 空闲 30 分钟滑动、绝对 12 小时、单用户 20 条，全部取自 core/auth。
// CSRF 令牌每次解析时重新派生，**不落盘**。

import (
	"context"
	"errors"
	"fmt"

	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
)

// SessionIssuer 是落库的会话签发器。
//
// Issue 在同一事务内写会话行与 auth.login 审计行：
// 二者要么都提交，要么都不提交。
type SessionIssuer struct {
	db       *DB
	sessions *SessionRepo
	audits   *AuditRepo
	clock    clock.Clock
	secret   []byte
}

// NewSessionIssuer 组合会话仓储与审计仓储。
//
// secret 是 CSRF 派生密钥。空值即错误：缺密钥的部署必须在启动时失败，
// 而不是发出一批无 CSRF 保护的会话。
func NewSessionIssuer(sessions *SessionRepo, audits *AuditRepo, secret []byte, clk clock.Clock) (*SessionIssuer, error) {
	if sessions == nil || sessions.db == nil {
		return nil, errors.New("pg: nil session repo")
	}
	if audits == nil {
		return nil, errors.New("pg: nil audit repo")
	}
	if len(secret) == 0 {
		return nil, errors.New("pg: empty CSRF secret")
	}
	if clk == nil {
		clk = clock.System{}
	}
	return &SessionIssuer{
		db:       sessions.db,
		sessions: sessions,
		audits:   audits,
		clock:    clk,
		secret:   append([]byte(nil), secret...),
	}, nil
}

// Issue 为 user 签发新会话。
//
// **总是签发新令牌**：登录前的令牌不复用。
// 会话行与 auth.login 审计行同事务提交，故不会出现「有会话无审计」。
func (s *SessionIssuer) Issue(ctx context.Context, user coreauth.User) (coreauth.Issued, error) {
	if user.ID == "" {
		return coreauth.Issued{}, errors.New("pg: session user id is required")
	}
	token, err := coreauth.NewToken()
	if err != nil {
		return coreauth.Issued{}, fmt.Errorf("pg: new session token: %w", err)
	}
	now := s.clock.Now().UTC()
	var issued coreauth.Issued
	err = s.db.InTx(ctx, func(ctx context.Context) error {
		row, err := s.sessions.Create(ctx, coreauth.NewSessionRow{
			UserID:        user.ID,
			TokenHash:     coreauth.HashToken(token),
			AuthMethod:    "password",
			Now:           now,
			IdleExpiresAt: now.Add(coreauth.SessionIdleWindow),
			ExpiresAt:     now.Add(coreauth.SessionAbsoluteWindow),
		})
		if err != nil {
			return err
		}
		if _, err := s.audits.Append(ctx, coreaudit.Entry{
			TS:          now,
			ActorUserID: user.ID,
			ActorRole:   user.Role,
			ActorLabel:  mask.EmailLabel(user.Email),
			Action:      coreaudit.ActionAuthLogin,
			TargetType:  "session",
			TargetID:    row.ID,
		}); err != nil {
			return err
		}
		issued = coreauth.Issued{
			Token: token,
			Session: coreauth.Session{
				User:      user,
				CSRFToken: coreauth.CSRFToken(s.secret, token),
			},
		}
		return nil
	})
	if err != nil {
		return coreauth.Issued{}, err
	}
	return issued, nil
}

// Resolve 返回 token 对应的会话，并滑动空闲窗口。
//
// 未知、已撤销、过期、用户停用或删除一律返回 coreauth.ErrNoSession。
// 其他存储错误原样上抛：入口层不能把数据库故障当成「未登录」。
func (s *SessionIssuer) Resolve(ctx context.Context, token string) (coreauth.Session, error) {
	if token == "" {
		return coreauth.Session{}, coreauth.ErrNoSession
	}
	resolved, err := s.sessions.Resolve(ctx, coreauth.HashToken(token),
		s.clock.Now().UTC(), coreauth.SessionIdleWindow)
	if err != nil {
		if errs.From(err).Code == errs.CodeNotFound {
			return coreauth.Session{}, coreauth.ErrNoSession
		}
		return coreauth.Session{}, err
	}
	return coreauth.Session{
		User:      resolved.User,
		CSRFToken: coreauth.CSRFToken(s.secret, token),
	}, nil
}

// Revoke 撤销一个会话。幂等。
func (s *SessionIssuer) Revoke(ctx context.Context, token string) error {
	if token == "" {
		return nil
	}
	return s.sessions.Revoke(ctx, coreauth.HashToken(token), s.clock.Now().UTC())
}

// RevokeAllForUser 撤销 userID 的全部有效会话。幂等。
func (s *SessionIssuer) RevokeAllForUser(ctx context.Context, userID string) error {
	if userID == "" {
		return errors.New("pg: user id is required")
	}
	_, err := s.sessions.RevokeAllForUser(ctx, userID, s.clock.Now().UTC())
	return err
}

// 编译期确认本类型满足入口端口。
var _ coreauth.SessionIssuer = (*SessionIssuer)(nil)
