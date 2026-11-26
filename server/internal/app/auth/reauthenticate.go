// 危险操作二次口令（reauth）。
//
// # 功能
// Reauthenticate 让当前会话用户在危险请求上重新提交**自己的登录口令**。
// 校验读用户、KDF.Verify 与 active 判定；用户不存在与坏口令同路径垫时。
// 失败每次写一条无 PII 审计；超限在同一窗口只写一条汇总审计，
// 其余 429 只记 Warn（界定了公开端点可被刷写的上界）。
// 本文件**不**做任何业务写入。
//
// # 归属
// app 层用例。入口层通过窄接口注入 account/user handler，
// 用例层不接触登录口令；口令不进日志。
//
// # 口径
// 错误统一 forbidden（errs.CodeForbidden）→ HTTP 403；
// 节流命中 rate_limited → HTTP 429，错误携带建议重试秒数，
// 入口层据此写 Retry-After。每用户 5 分钟窗口内失败 5 次后，
// 后续尝试一律 429，**不**再付 KDF 成本；成功一次即清零。
// 占坑与判限在同一把锁内完成（reserve），并发不可越过上限。
// 基础设施错误（仓储、KDF 故障）撤销占坑：服务器故障不算用户的失败。
// 哈希串不可读是数据事故，与登录同口径回 internal，不伪装成 403。

package auth

import (
	"context"
	"sync"
	"time"

	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/logfield"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
)

// ReauthFailureLimit 是窗口内允许的失败次数；第 limit+1 次尝试一律 429。
const ReauthFailureLimit = 5

// ReauthWindow 是失败计数的滑动窗口。
const ReauthWindow = 5 * time.Minute

// ErrReauthFailed 是二次口令错误的唯一错误。
// 与登录失败同口径：不区分「用户不存在」与「口令错误」；
// 每次失败写一条 auth.reauth_failed 审计，但审计内容不含口令或邮箱。
var ErrReauthFailed = errs.New(errs.CodeForbidden, "reauthentication failed")

// ErrReauthThrottledMessage 是节流错误的稳定文案。
const ErrReauthThrottledMessage = "too many failed reauth attempts"

// ReauthThrottled 是节流命中的 typed 错误。
// 包装 rate_limited 稳定错误（Unwrap 供 errs.From 按码映射 429）；
// RetryAfter 是建议重试时长，入口层据此写 Retry-After 头。
type ReauthThrottled struct {
	err        *errs.Error
	RetryAfter time.Duration
}

// NewReauthThrottled 构造节流错误：稳定码 rate_limited 加建议重试时长。
// 入口替身与测试共用本构造，字段保持私有。
func NewReauthThrottled(retryAfter time.Duration) *ReauthThrottled {
	return &ReauthThrottled{
		err:        errs.New(errs.CodeRateLimited, ErrReauthThrottledMessage),
		RetryAfter: retryAfter,
	}
}

// Error 实现 error。
func (e *ReauthThrottled) Error() string { return e.err.Error() }

// Unwrap 暴露稳定错误：respond 按 rate_limited 码回 429。
func (e *ReauthThrottled) Unwrap() error { return e.err }

// reauthFailures 记录一个用户的失败（占坑）时刻。
// throttledAudited 标记本窗口的首条 429 汇总审计是否已放行：
// 同一窗口只写一条，成功校验 clear 后随状态一起消失。
type reauthFailures struct {
	at               []time.Time
	throttledAudited bool
}

// reauthThrottle 是进程内按用户的失败节流。
// 单进程内存实现：多副本部署时各副本独立计数，窗口仍封顶单副本爆破。
type reauthThrottle struct {
	mu     sync.Mutex
	fails  map[string]*reauthFailures
	clock  clock.Clock
	limit  int
	window time.Duration
}

func newReauthThrottle(clk clock.Clock, limit int, window time.Duration) *reauthThrottle {
	return &reauthThrottle{
		fails:  map[string]*reauthFailures{},
		clock:  clk,
		limit:  limit,
		window: window,
	}
}

// reserve 原子占坑：清窗、判限与递增在同一把锁内完成。
// 并发请求最多放行 limit 个坑位，其余不得付 KDF 成本。
// ok=false 表示已达上限；wait 是最早一个坑位离开窗口的剩余时长；
// count 是窗口内的失败条数（ok=false 时停在 limit）；
// auditThrottle=true 表示本次是窗口内首次超限、应写唯一一条 429 汇总审计。
// 标记在锁内置位，故并发超限最多一个调用看到 true。
func (t *reauthThrottle) reserve(userID string) (wait time.Duration, count int, ok bool, auditThrottle bool) {
	t.mu.Lock()
	defer t.mu.Unlock()
	now := t.clock.Now().UTC()
	state := t.fails[userID]
	if state != nil && state.prune(now, t.window) == 0 {
		delete(t.fails, userID)
		state = nil
	}
	if state != nil && len(state.at) >= t.limit {
		wait := t.window - now.Sub(state.at[0])
		if wait < 0 {
			wait = 0
		}
		auditThrottle = !state.throttledAudited
		if auditThrottle {
			state.throttledAudited = true
		}
		return wait, len(state.at), false, auditThrottle
	}
	if state == nil {
		state = &reauthFailures{}
		t.fails[userID] = state
	}
	state.at = append(state.at, now)
	state.throttledAudited = false
	return 0, len(state.at), true, false
}

// release 撤销一次占坑。基础设施错误不是用户的失败，不得计入。
func (t *reauthThrottle) release(userID string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	state, ok := t.fails[userID]
	if !ok || len(state.at) == 0 {
		return
	}
	state.at = state.at[:len(state.at)-1]
	if len(state.at) == 0 {
		delete(t.fails, userID)
	}
}

// clear 清掉该用户的全部失败计数。成功校验后调用。
func (t *reauthThrottle) clear(userID string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	delete(t.fails, userID)
}

// rollbackThrottleAudit 撤销「本窗口已写汇总审计」的标记。
// 仅在审计写失败时调用：允许同窗口的后续超限重试写一次，
// 并发下宁可重复也不漏记。成功写入后标记保持置位。
func (t *reauthThrottle) rollbackThrottleAudit(userID string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if state, ok := t.fails[userID]; ok {
		state.throttledAudited = false
	}
}

// prune 原地剔除窗口外时刻，返回保留条数。
func (f *reauthFailures) prune(now time.Time, window time.Duration) int {
	kept := f.at[:0]
	for _, ts := range f.at {
		if now.Sub(ts) < window {
			kept = append(kept, ts)
		}
	}
	f.at = kept
	return len(kept)
}

// Reauthenticate 校验当前会话用户的登录口令。
//
// userID 是会话主体；password 是它重新提交的登录口令。
// 用户不存在时仍执行一次等价校验压平计时，再统一回 ErrReauthFailed。
// 节流：付 KDF 成本**之前**原子占坑；窗口内失败达 ReauthFailureLimit 后，
// 本方法回 *ReauthThrottled（rate_limited + Retry-After 秒数），
// 不付 KDF 成本；校验成功清零计数。
// 每次 403 写一条 auth.reauth_failed 审计；同一 5 分钟窗口的首次 429
// 再写一条汇总审计（detail 无口令、无邮箱），其后只 Warn。
// 审计写失败即返回错误，不把未留痕的失败伪装成 403/429；
// 汇总审计失败会回滚标记，让同窗口的下一次超限重试补写。
// 日志只记 user_id 与结果，口令不出现在任何日志字段里。
func (s *Service) Reauthenticate(ctx context.Context, userID, password string) error {
	if password == "" {
		// 缺口令是入口层判 400 的形状；走到这里按缺值处理。
		return errs.New(errs.CodeValidation, "current_password is required")
	}
	wait, count, ok, auditThrottle := s.reauth.reserve(userID)
	if !ok {
		if auditThrottle {
			// 窗口内首次超限：写唯一一条汇总审计。写失败回滚标记并失败关闭，
			// 让同窗口的下一次超限重试补写，而不是把 429 当成功。
			role, label := s.reauthActor(ctx, userID)
			if err := s.recordReauthFailure(ctx, userID, role, label, count, "throttled"); err != nil {
				s.reauth.rollbackThrottleAudit(userID)
				return err
			}
		} else {
			s.logReauthThrottled(userID, count)
		}
		return NewReauthThrottled(wait)
	}
	rec, err := s.users.GetByID(ctx, userID)
	if err != nil {
		if !isNotFound(err) {
			s.reauth.release(userID)
			return err
		}
		if err := s.verifyDummyPassword(ctx, password); err != nil {
			s.reauth.release(userID)
			return err
		}
		s.logReauth(userID, "unknown_user")
		if err := s.recordReauthFailure(ctx, userID, "", mask.EmailLabel(""), count, "invalid"); err != nil {
			return err
		}
		return ErrReauthFailed
	}
	check, err := s.kdf.Verify(ctx, password, rec.PasswordHash)
	if err != nil {
		// 坏哈希是数据事故，不是口令错误；与登录同口径回 internal。
		s.reauth.release(userID)
		return errs.New(errs.CodeInternal, "stored hash is unreadable")
	}
	if !check.Valid || rec.Status != coreauth.StatusActive {
		s.logReauth(userID, "invalid_credentials")
		if err := s.recordReauthFailure(ctx, userID, rec.Role, mask.EmailLabel(rec.Email), count, "invalid"); err != nil {
			return err
		}
		return ErrReauthFailed
	}
	s.reauth.clear(userID)
	return nil
}

// reauthActor 取审计行主体。用户不存在或读失败时回退到无角色与 unknown 标签：
// 失败审计不能因为主体读不到而不落。
func (s *Service) reauthActor(ctx context.Context, userID string) (coreauth.Role, string) {
	rec, err := s.users.GetByID(ctx, userID)
	if err != nil {
		return "", mask.EmailLabel("")
	}
	return rec.Role, mask.EmailLabel(rec.Email)
}

// recordReauthFailure 写一条 auth.reauth_failed。
// detail 只含字段名、状态词与失败计数：from 是尝试前未知的旧状态，
// to 是 invalid/throttled，count 是窗口内失败条数。**绝无口令或邮箱**。
func (s *Service) recordReauthFailure(ctx context.Context, userID string, role coreauth.Role, label string, count int, outcome string) error {
	now := s.clock.Now().UTC()
	return s.tx.InTx(ctx, func(ctx context.Context) error {
		_, err := s.audits.Append(ctx, coreaudit.Entry{
			TS:          now,
			ActorUserID: userID,
			ActorRole:   role,
			ActorLabel:  label,
			Action:      coreaudit.ActionAuthReauthFailed,
			TargetType:  "user",
			TargetID:    userID,
			Detail: map[string]any{
				"field": "current_password",
				"from":  "unknown",
				"to":    outcome,
				"count": count,
			},
		})
		return err
	})
}

// logReauth 记一条失败事件。只带 user_id 与 reason，**永不带口令**。
func (s *Service) logReauth(userID, reason string) {
	s.log.Info("appauth: reauth failed",
		logfield.KeyUserID, userID,
		logfield.KeyReason, reason)
}

// logReauthThrottled 记一条被抑制的 429。只带 user_id 与窗口计数，
// **永不带口令或邮箱**；窗口内首次超限另有一条汇总审计。
func (s *Service) logReauthThrottled(userID string, count int) {
	s.log.Warn("appauth: reauth throttled",
		logfield.KeyUserID, userID,
		logfield.KeyCount, count)
}
