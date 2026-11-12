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
// 重置令牌取 1 小时：足够用户查收邮件，又限制泄露窗口。
const ResetTokenTTL = time.Hour

// ErrInvalidCredentials 是登录失败的同形错误。
// **用户不存在与口令错误共用本值**，文案也相同。
var ErrInvalidCredentials = errs.New(errs.CodeUnauthorized, "invalid email or password")

// recoverableLoginError 构造冷静期登录引导（clc，对齐旧线 web/routes/auth.py）：
// 本人软删且在 7 天恢复窗内、口令正确时，登录不放行也不建会话，
// 回同一条 401 并在 details 增补 recoverable/recover_until，引导走
// POST /api/me/restore。code 与 message 与不存在同形；recover_until
// 是恢复窗终点（RFC3339，注销时刻＋7 天）。口令错误**不**得到本形状——
// 否则公开登录端点变成近期注销邮箱的枚举器；旧线同样只在口令验对后引导。
func recoverableLoginError(recoverUntil time.Time) *errs.Error {
	return &errs.Error{
		Code:    errs.CodeUnauthorized,
		Message: ErrInvalidCredentials.Message,
		Details: map[string]any{
			"recoverable": true,
			// RFC3339 带小数秒：与注销响应里 date-time 的序列化同源同形
			// （time.Time 的 JSON 编码即 RFC3339Nano，尾零省略）。
			"recover_until": recoverUntil.UTC().Format(time.RFC3339Nano),
		},
	}
}

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
	// Registrations 是全局注册开关。公开注册读它，缺依赖即构造失败。
	Registrations RegistrationStore
	Audits        appaudit.Repo
	Tx            app.TxManager
	Clock         clock.Clock
	// Quota 提供生效的活跃用户/账号上限（0=不限）。必填：
	// 注册建号与自助恢复在锁下读同一份生效值，缺失即构造失败。
	Quota app.QuotaLimits
	// KDF 是口令哈希端口。生产必须传限并发实现（coreauth.Bounded）；
	// 测试可传 PlainHasher 或计数器替身。nil 即构造失败。
	KDF coreauth.Hasher
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
	users         appuser.Repo
	sessions      coreauth.SessionIssuer
	others        OtherSessionRevoker
	resets        PasswordResetRepo
	registrations RegistrationStore
	audits        appaudit.Repo
	tx            app.TxManager
	clock         clock.Clock
	quota         app.QuotaLimits
	kdf           coreauth.Hasher
	mail          coremail.Sender
	mailBase      string
	log           *slog.Logger
	// mailSlots 限并发投递数。满即丢投递并记日志，**不阻塞用例**：
	// 公开端点不得因 SMTP 慢而拖住响应。
	mailSlots chan struct{}
	mailWG    sync.WaitGroup
	// throttle 按归一邮箱限制重置请求频率（见 ForgotCooldown）。
	throttle resetThrottle
	// reauth 按用户限制危险操作二次口令的失败次数（见 reauthenticate.go）。
	reauth *reauthThrottle
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
	case d.Registrations == nil:
		return nil, errors.New("appauth: nil registration store")
	case d.Audits == nil:
		return nil, errors.New("appauth: nil audit repo")
	case d.Tx == nil:
		return nil, errors.New("appauth: nil transaction manager")
	case d.Clock == nil:
		return nil, errors.New("appauth: nil clock")
	case d.KDF == nil:
		return nil, errors.New("appauth: nil KDF")
	case d.Quota == nil:
		return nil, errors.New("appauth: nil quota limits")
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
		users:         d.Users,
		sessions:      d.Sessions,
		others:        d.OtherSessions,
		resets:        d.Resets,
		registrations: d.Registrations,
		audits:        d.Audits,
		tx:            d.Tx,
		clock:         d.Clock,
		quota:         d.Quota,
		kdf:           d.KDF,
		mail:          d.Mail,
		mailBase:      mailBase,
		log:           log,
		mailSlots:     make(chan struct{}, maxConcurrentResetMails),
		reauth:        newReauthThrottle(d.Clock, ReauthFailureLimit, ReauthWindow),
	}, nil
}

// Login 校验口令并签发会话。
//
// 口令错误与账号不存在返回同一条 ErrInvalidCredentials：
// 用户不存在时仍执行一次 argon2id 校验，压平计时差异。
// 本人软删且在 7 天恢复窗内、口令正确时回同形 401＋details 引导
// （recoverable/recover_until，见 recoverableLoginError），不建会话；
// 超窗与管理员删除不带引导，与不存在同形。
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
		handled, herr := s.checkSelfDeleteGrace(ctx, email, password, now)
		if herr != nil {
			return coreauth.Issued{}, herr
		}
		if handled {
			return coreauth.Issued{}, ErrInvalidCredentials
		}
		if err := s.verifyDummyPassword(ctx, password); err != nil {
			return coreauth.Issued{}, err
		}
		if err := s.recordLoginFailure(ctx, "", coreauth.RoleUser, email, "invalid_credentials", now); err != nil {
			return coreauth.Issued{}, err
		}
		return coreauth.Issued{}, ErrInvalidCredentials
	}

	check, err := s.kdf.Verify(ctx, password, rec.PasswordHash)
	if err != nil {
		// 坏哈希是数据事故，不是口令错误。不把它伪装成 401，
		// 否则迁移出错会表现成「全员口令错」。
		return coreauth.Issued{}, fmt.Errorf("appauth: stored hash for %s is unreadable: %w", rec.ID, err)
	}
	if !check.Valid || rec.Status != coreauth.StatusActive {
		if err := s.recordLoginFailure(ctx, rec.ID, rec.Role, email, "invalid_credentials", now); err != nil {
			return coreauth.Issued{}, err
		}
		return coreauth.Issued{}, ErrInvalidCredentials
	}

	if check.NeedsRehash {
		upgraded, err := s.kdf.Hash(ctx, password)
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
// reason 区分形状：invalid_credentials 是口令/存在性失败；recoverable 是
// 冷静期内口令已验对但未建会话——旧线同样为这一刻意状态单独留痕
// （login_recoverable），否则公开登录端点在恢复窗内是一台零留痕的
// 凭据验证器。两者共用动作名，成组取证口径不变。
func (s *Service) recordLoginFailure(ctx context.Context, userID string, role coreauth.Role, email, reason string, now time.Time) error {
	return s.tx.InTx(ctx, func(ctx context.Context) error {
		_, err := s.audits.Append(ctx, coreaudit.Entry{
			TS:          now,
			ActorUserID: userID,
			ActorRole:   role,
			ActorLabel:  mask.EmailLabel(email),
			Action:      coreaudit.ActionAuthLoginFailed,
			TargetType:  "user",
			TargetID:    userID,
			Detail:      map[string]any{"reason": reason},
		})
		return err
	})
}

// checkSelfDeleteGrace 在活跃行查不到之后检查软删行（旧线 find_user_any 同位）。
// 返回值 handled=false 表示无软删行，调用方走「不存在」原路径；
// handled=true 表示已按软删行完成校验与留痕，err 非 nil 时为该次的最终错误
// （recoverableLoginError），err 为 nil 时调用方回同一条 ErrInvalidCredentials。
// 校验成本与「不存在」路径同量级：两条路径都恰好付一次 KDF 校验。
func (s *Service) checkSelfDeleteGrace(ctx context.Context, email, password string, now time.Time) (bool, error) {
	rec, err := s.users.FindDeletedByEmail(ctx, email)
	if err != nil {
		if isNotFound(err) {
			return false, nil
		}
		return true, err
	}
	check, err := s.kdf.Verify(ctx, password, rec.PasswordHash)
	if err != nil {
		return true, fmt.Errorf("appauth: stored hash for %s is unreadable: %w", rec.ID, err)
	}
	// 引导恢复只在自助恢复入口真的可用时出现：本人软删、窗内、口令正确。
	// 超窗、管理员删除、口令错一律与「不存在」同形，不给任何额外信号。
	if check.Valid && rec.DeletedAt != nil && rec.DeletedBy == rec.ID {
		until := rec.DeletedAt.Add(coreuser.SelfRestoreGrace)
		if !now.After(until) {
			if ferr := s.recordLoginFailure(ctx, rec.ID, rec.Role, email, "recoverable", now); ferr != nil {
				return true, ferr
			}
			return true, recoverableLoginError(until)
		}
	}
	if ferr := s.recordLoginFailure(ctx, rec.ID, rec.Role, email, "invalid_credentials", now); ferr != nil {
		return true, ferr
	}
	return true, nil
}

// dummyOnce 生成计时拉平用的哈希。每个进程只算一次。
// 一旦生成就缓存：首个请求的取消不得把错误缓存给全进程。
var dummyOnce struct {
	sync.Once
	hash string
	err  error
}

// dummyHash 返回计时拉平哈希。首次调用走本服务的 KDF 闸。
func (s *Service) dummyHash(ctx context.Context) (string, error) {
	dummyOnce.Do(func() {
		dummyOnce.hash, dummyOnce.err = s.kdf.Hash(
			context.WithoutCancel(ctx), "yiban-login-timing-placeholder")
	})
	return dummyOnce.hash, dummyOnce.err
}

// verifyDummyPassword 对不存在的用户执行一次等价校验。
// 结果**必然**不匹配；唯一目的是消耗与真实校验同样的时间。
// 走同一 Hasher，故它也受进程级 KDF 并发上限约束。
func (s *Service) verifyDummyPassword(ctx context.Context, password string) error {
	hash, err := s.dummyHash(ctx)
	if err != nil {
		return err
	}
	_, err = s.kdf.Verify(ctx, password, hash)
	return err
}

func isNotFound(err error) bool {
	return errs.From(err).Code == errs.CodeNotFound
}
