// 登录用例与 Service 定义。
//
// 包注释见 repo.go。本文件只放登录路径、计时拉平与输入归一。

package auth

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/user"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coremail "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mail"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// ResetTokenTTL 是一次性重置令牌的有效期。
// P1 取 1 小时：足够用户查收邮件，又限制泄露窗口。
const ResetTokenTTL = time.Hour

// ErrInvalidCredentials 是登录失败的唯一错误。
// **用户不存在与口令错误共用本值**，文案也相同。
var ErrInvalidCredentials = errs.New(errs.CodeUnauthorized, "invalid email or password")

// ErrInvalidResetToken 表示重置令牌未知、已用或过期。
// 三种情形共用本值，**不区分**。
var ErrInvalidResetToken = errs.New(errs.CodeUnauthorized, "invalid or expired reset token")

// Deps 是 Service 的依赖。除 Mail 外全部必填，缺一即构造失败。
type Deps struct {
	Users    appuser.Repo
	Sessions coreauth.SessionIssuer
	// OtherSessions 撤销本人其余会话，供改密使用。
	OtherSessions OtherSessionRevoker
	Resets        PasswordResetRepo
	Audits        appaudit.Repo
	Tx            app.TxManager
	Clock         clock.Clock
	// Mail 投递重置邮件。为 nil 表示 SMTP 未配置：
	// 令牌照常生成，投递跳过，并留一条显性「未投递」日志。
	Mail coremail.Sender
	// MailBaseURL 是重置链接基址（无尾斜杠）。Mail 非 nil 时必填。
	MailBaseURL string
	// Logger 可选。缺省用 slog.Default。
	Logger *slog.Logger
}

// Service 承载认证用例。
// 零值不可用；请用 NewService 构造。
type Service struct {
	users    appuser.Repo
	sessions coreauth.SessionIssuer
	others   OtherSessionRevoker
	resets   PasswordResetRepo
	audits   appaudit.Repo
	tx       app.TxManager
	clock    clock.Clock
	mail     coremail.Sender
	mailBase string
	log      *slog.Logger
	// mailSlots 限并发投递数。满即丢投递并记日志，**不阻塞用例**：
	// 公开端点不得因 SMTP 慢而拖住响应。
	mailSlots chan struct{}
	mailWG    sync.WaitGroup
	// throttle 按归一邮箱限制重置请求频率（见 ForgotCooldown）。
	throttle resetThrottle
}

// NewService 构造用例服务。任一必需依赖缺失即失败关闭。
func NewService(d Deps) (*Service, error) {
	switch {
	case d.Users == nil:
		return nil, errors.New("appauth: nil user repo")
	case d.Sessions == nil:
		return nil, errors.New("appauth: nil session issuer")
	case d.OtherSessions == nil:
		return nil, errors.New("appauth: nil other-session revoker")
	case d.Resets == nil:
		return nil, errors.New("appauth: nil password reset repo")
	case d.Audits == nil:
		return nil, errors.New("appauth: nil audit repo")
	case d.Tx == nil:
		return nil, errors.New("appauth: nil transaction manager")
	case d.Clock == nil:
		return nil, errors.New("appauth: nil clock")
	}
	mailBase := strings.TrimRight(strings.TrimSpace(d.MailBaseURL), "/")
	if d.Mail != nil && mailBase == "" {
		return nil, errors.New("appauth: MailBaseURL is required when Mail is configured")
	}
	log := d.Logger
	if log == nil {
		log = slog.Default()
	}
	return &Service{
		users:     d.Users,
		sessions:  d.Sessions,
		others:    d.OtherSessions,
		resets:    d.Resets,
		audits:    d.Audits,
		tx:        d.Tx,
		clock:     d.Clock,
		mail:      d.Mail,
		mailBase:  mailBase,
		log:       log,
		mailSlots: make(chan struct{}, maxConcurrentResetMails),
	}, nil
}

// Login 校验口令并签发会话。
//
// 失败一律返回 ErrInvalidCredentials：
// 用户不存在时仍执行一次 argon2id 校验，压平计时差异。
// 旧 scrypt 哈希校验成功后**当场**重哈希为 argon2id。
// 成功时 auth.login 审计由 SessionIssuer 同事务写入。
func (s *Service) Login(ctx context.Context, email, password string) (coreauth.Issued, error) {
	email = coreuser.NormalizeEmail(email)
	if !coreuser.ValidEmail(email) {
		return coreauth.Issued{}, errs.New(errs.CodeValidation, "invalid email")
	}
	if password == "" {
		return coreauth.Issued{}, errs.New(errs.CodeValidation, "password is required")
	}
	now := s.clock.Now().UTC()

	rec, err := s.users.GetByEmail(ctx, email)
	if err != nil {
		if !isNotFound(err) {
			return coreauth.Issued{}, err
		}
		if err := verifyDummyPassword(password); err != nil {
			return coreauth.Issued{}, err
		}
		if err := s.recordLoginFailure(ctx, "", coreauth.RoleUser, email, now); err != nil {
			return coreauth.Issued{}, err
		}
		return coreauth.Issued{}, ErrInvalidCredentials
	}

	check, err := coreauth.VerifyPassword(password, rec.PasswordHash)
	if err != nil {
		// 坏哈希是数据事故，不是口令错误。不把它伪装成 401，
		// 否则迁移出错会表现成「全员口令错」。
		return coreauth.Issued{}, fmt.Errorf("appauth: stored hash for %s is unreadable: %w", rec.ID, err)
	}
	if !check.Valid || rec.Status != coreauth.StatusActive {
		if err := s.recordLoginFailure(ctx, rec.ID, rec.Role, email, now); err != nil {
			return coreauth.Issued{}, err
		}
		return coreauth.Issued{}, ErrInvalidCredentials
	}

	if check.NeedsRehash {
		upgraded, err := coreauth.HashPassword(password)
		if err != nil {
			return coreauth.Issued{}, err
		}
		if err := s.users.RehashPassword(ctx, rec.ID, upgraded); err != nil {
			return coreauth.Issued{}, err
		}
	}
	if err := s.users.MarkLogin(ctx, rec.ID, now); err != nil {
		return coreauth.Issued{}, err
	}
	return s.sessions.Issue(ctx, coreauth.User{
		ID:                 rec.ID,
		Email:              rec.Email,
		Role:               rec.Role,
		Status:             rec.Status,
		MustChangePassword: rec.MustChangePassword,
	})
}

// recordLoginFailure 写 auth.login_failed。
// 审计写失败即返回错误：**不得**在证据没落下时回一个 401。
func (s *Service) recordLoginFailure(ctx context.Context, userID string, role coreauth.Role, email string, now time.Time) error {
	return s.tx.InTx(ctx, func(ctx context.Context) error {
		_, err := s.audits.Append(ctx, coreaudit.Entry{
			TS:          now,
			ActorUserID: userID,
			ActorRole:   role,
			ActorLabel:  mask.EmailLabel(email),
			Action:      coreaudit.ActionAuthLoginFailed,
			TargetType:  "user",
			TargetID:    userID,
			Detail:      map[string]any{"reason": "invalid_credentials"},
		})
		return err
	})
}

// dummyOnce 生成计时拉平用的哈希。每个进程只算一次。
var dummyOnce = sync.OnceValues(func() (string, error) {
	return coreauth.HashPassword("yiban-login-timing-placeholder")
})

// verifyDummyPassword 对不存在的用户执行一次等价校验。
// 结果**必然**不匹配；唯一目的是消耗与真实校验同样的时间。
func verifyDummyPassword(password string) error {
	hash, err := dummyOnce()
	if err != nil {
		return err
	}
	_, err = coreauth.VerifyPassword(password, hash)
	return err
}

func isNotFound(err error) bool {
	return errs.From(err).Code == errs.CodeNotFound
}
