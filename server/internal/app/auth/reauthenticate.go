// 危险操作二次口令（reauth）。
//
// # 功能
// Reauthenticate 让当前会话用户在危险请求上重新提交**自己的登录口令**。
// 校验读用户、KDF.Verify 与 active 判定；用户不存在与坏口令同路径垫时。
// 本文件**不**写审计、**不**做任何业务写入：失败只累计内存节流计数。
//
// # 归属
// app 层用例。入口层通过窄接口注入 account/user handler，
// 用例层不接触登录口令；口令不进日志。
//
// # 口径
// 错误统一 forbidden（errs.CodeForbidden）→ HTTP 403；
// 节流命中 rate_limited → HTTP 429。每用户 5 分钟窗口内失败 5 次后，
// 后续尝试一律 429，**不**再付 KDF 成本；成功一次即清零。
// 哈希串不可读是数据事故，与登录同口径回 internal，不伪装成 403。

package auth

import (
	"context"
	"sync"
	"time"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/logfield"
)

// ReauthFailureLimit 是窗口内允许的失败次数；第 limit+1 次尝试一律 429。
const ReauthFailureLimit = 5

// ReauthWindow 是失败计数的滑动窗口。
const ReauthWindow = 5 * time.Minute

// ErrReauthFailed 是二次口令错误的唯一错误。
// 与登录失败不同：这里不区分「用户不存在」与「口令错误」，且**不写审计**。
var ErrReauthFailed = errs.New(errs.CodeForbidden, "reauthentication failed")

// reauthFailures 记录一个用户的失败时刻。
type reauthFailures struct {
	at []time.Time
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

// blocked 报告该用户当前是否已达失败上限。
// 窗口内失败**已达** limit 即拦：第 6 次尝试（哪怕口令正确）一律 429。
func (t *reauthThrottle) blocked(userID string) bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	state, ok := t.fails[userID]
	if !ok {
		return false
	}
	now := t.clock.Now().UTC()
	kept := state.prune(now, t.window)
	if kept == 0 {
		delete(t.fails, userID)
		return false
	}
	return kept >= t.limit
}

// record 记一次失败；返回当前窗口内失败次数。
func (t *reauthThrottle) record(userID string) int {
	t.mu.Lock()
	defer t.mu.Unlock()
	state, ok := t.fails[userID]
	if !ok {
		state = &reauthFailures{}
		t.fails[userID] = state
	}
	now := t.clock.Now().UTC()
	state.prune(now, t.window)
	state.at = append(state.at, now)
	return len(state.at)
}

// clear 清掉该用户的全部失败计数。成功校验后调用。
func (t *reauthThrottle) clear(userID string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	delete(t.fails, userID)
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
// 节流：窗口内失败达 ReauthFailureLimit 后，本方法直接回
// rate_limited，不付 KDF 成本；校验成功清零计数。
// 本方法**不写库、不写审计**：日志只记 user_id 与结果，口令不出现在
// 任何日志字段里。
func (s *Service) Reauthenticate(ctx context.Context, userID, password string) error {
	if password == "" {
		// 缺口令是入口层判 400 的形状；走到这里按缺值处理。
		return errs.New(errs.CodeValidation, "current_password is required")
	}
	if s.reauth.blocked(userID) {
		return errs.New(errs.CodeRateLimited, "too many failed reauth attempts")
	}
	rec, err := s.users.GetByID(ctx, userID)
	if err != nil {
		if !isNotFound(err) {
			return err
		}
		if err := s.verifyDummyPassword(ctx, password); err != nil {
			return err
		}
		s.reauth.record(userID)
		s.logReauth(userID, "unknown_user")
		return ErrReauthFailed
	}
	check, err := s.kdf.Verify(ctx, password, rec.PasswordHash)
	if err != nil {
		// 坏哈希是数据事故，不是口令错误；与登录同口径回 internal。
		return errs.New(errs.CodeInternal, "stored hash is unreadable")
	}
	if !check.Valid || rec.Status != coreauth.StatusActive {
		s.reauth.record(userID)
		s.logReauth(userID, "invalid_credentials")
		return ErrReauthFailed
	}
	s.reauth.clear(userID)
	return nil
}

// logReauth 记一条失败事件。只带 user_id 与 reason，**永不带口令**。
func (s *Service) logReauth(userID, reason string) {
	s.log.Info("appauth: reauth failed",
		logfield.KeyUserID, userID,
		logfield.KeyReason, reason)
}
