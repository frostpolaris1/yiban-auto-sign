// 忘记口令的邮件投递与节流。
//
// # 功能
// 把一次性重置令牌拼成重置链接，交给 core/mail.Sender 投递；
// 按归一邮箱节流，防止同一邮箱被当邮件炸弹。
// 本文件**不**生成令牌、**不**写数据库、**不**实现 SMTP。
//
// # 归属
// app/auth 用例层。投递端口由 core/mail 声明，实现由 adapters/mail 提供。
//
// # 复用
// ForgotCooldown、maxConcurrentResetMails、resetThrottle；
// resetTTLText、resetMailText；
// Service.deliverReset、Service.resetLink、Service.Close。
//
// # 口径
// 投递在后台 goroutine 里进行：公开端点不得因 SMTP 慢而拖住响应，
// 否则响应时延会重新暴露"邮箱是否存在"（已知邮箱要等 SMTP）。
// 并发投递上限 maxConcurrentResetMails；满即丢弃并记一条显性日志，不排队。
// 失败只记粗分类（core/mail.Kind）；**令牌、口令、完整邮箱不进日志**。
package auth

import (
	"context"
	"fmt"
	"net/url"
	"sync"
	"time"

	coremail "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mail"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
)

// ForgotCooldown 是同一归一邮箱的两次重置请求之间的最小间隔。
//
// 阈值取 60s：用户误点两次不会收到两封信；把任意邮箱当邮件炸弹时，
// 投递速率被压到每分钟一封。限速只影响投递与令牌生成，
// **不改变响应**：被限速的请求仍付一次等价 KDF，仍返回成功。
const ForgotCooldown = 60 * time.Second

// maxConcurrentResetMails 是同时在途的投递 goroutine 上限。
// 取 4：SMTP 单连接预算 10s，最坏积压约 40s 的邮件量；
// 满即丢弃并记日志，不排队、不阻塞请求。
const maxConcurrentResetMails = 4

// maxThrottleEntries 是节流表的内存上限。
// 只有已知邮箱会进表；上限防止被大量真实邮箱撑爆内存。
// 到顶先清过期项；仍满则删最早一条（放过一次多余投递，换内存有界）。
const maxThrottleEntries = 4096

// resetThrottle 是按归一小写邮箱的冷却表。并发安全。
type resetThrottle struct {
	mu   sync.Mutex
	seen map[string]time.Time
}

// allow 报告该邮箱本次是否可投递，并登记本次时间。
// 被拒时不更新登记时间：连续请求从第一次开始计 60s。
func (t *resetThrottle) allow(email string, now time.Time) bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.seen == nil {
		t.seen = make(map[string]time.Time)
	}
	if last, ok := t.seen[email]; ok && now.Sub(last) < ForgotCooldown {
		return false
	}
	if len(t.seen) >= maxThrottleEntries {
		t.evict(now)
	}
	t.seen[email] = now
	return true
}

// evict 清过期项；仍满则删最早一条。
func (t *resetThrottle) evict(now time.Time) {
	for email, at := range t.seen {
		if now.Sub(at) >= ForgotCooldown {
			delete(t.seen, email)
		}
	}
	if len(t.seen) < maxThrottleEntries {
		return
	}
	var oldestEmail string
	var oldest time.Time
	for email, at := range t.seen {
		if oldestEmail == "" || at.Before(oldest) {
			oldestEmail, oldest = email, at
		}
	}
	delete(t.seen, oldestEmail)
}

// resetMailSubject 是重置邮件的主题。
const resetMailSubject = "重置口令"

// resetMailBodyFmt 是纯文本正文模板。
// %[1]s 是有效期文案，由 ResetTokenTTL 派生（见 resetTTLText）。
// %[2]s 是完整重置链接，令牌只出现在这里。
const resetMailBodyFmt = "有人为这个邮箱申请了重置口令。\n\n" +
	"打开以下链接设置新口令（%[1]s）：\n%[2]s\n\n" +
	"如果这不是你发起的，可以忽略本邮件；当前口令不会改变。\n"

// resetTTLText 把令牌有效期写成正文文案。
//
// 输入与 ResetTokenTTL 同源：令牌过期时间与正文说法不得各自写死。
// 整小时与整分钟按整数写；其他形状退成「尽快使用」。
func resetTTLText(ttl time.Duration) string {
	switch {
	case ttl >= time.Hour && ttl%time.Hour == 0:
		return fmt.Sprintf("%d 小时内有效", ttl/time.Hour)
	case ttl >= time.Minute && ttl%time.Minute == 0:
		return fmt.Sprintf("%d 分钟内有效", ttl/time.Minute)
	default:
		return "尽快使用"
	}
}

// resetMailText 返回重置邮件的正文。
// ttl 必须与写入令牌的有效期同源，否则正文说法与实际过期时间不一致。
func resetMailText(link string, ttl time.Duration) string {
	return fmt.Sprintf(resetMailBodyFmt, resetTTLText(ttl), link)
}

// deliverReset 把重置链接交给邮件端口。
//
// 未配置 SMTP：留一条显性「令牌已生成但未投递」警告并返回。
// 投递在后台进行；失败只记粗分类。两种情况都不返回错误：
// 调用方是公开端点，投递结果不得改变响应。
func (s *Service) deliverReset(to, token string) {
	if s.mail == nil {
		s.log.Warn("password reset token created but not delivered: SMTP is not configured",
			"event", "password_reset.not_delivered",
			"reason", "smtp_not_configured",
			"email", mask.EmailLabel(to))
		return
	}
	msg := coremail.Message{
		To:      to,
		Subject: resetMailSubject,
		Text:    resetMailText(s.resetLink(token), ResetTokenTTL),
	}
	select {
	case s.mailSlots <- struct{}{}:
	default:
		s.log.Warn("password reset token created but not delivered: delivery slots are full",
			"event", "password_reset.not_delivered",
			"reason", "delivery_queue_full",
			"email", mask.EmailLabel(to))
		return
	}
	s.mailWG.Add(1)
	go func() {
		defer s.mailWG.Done()
		defer func() { <-s.mailSlots }()
		// 投递实现 panic 不得拖垮进程；按失败记账，panic 值不落日志。
		// KindPanic 是 core/mail 词表里的兜底类，不可重试。
		defer func() {
			if r := recover(); r != nil {
				s.log.Error("password reset mail delivery panicked",
					"event", "password_reset.delivery_failed",
					"kind", string(coremail.KindPanic),
					"retryable", false,
					"email", mask.EmailLabel(to))
			}
		}()
		if err := s.mail.Send(context.Background(), msg); err != nil {
			kind, retryable := coremail.Classify(err)
			s.log.Warn("password reset mail delivery failed",
				"event", "password_reset.delivery_failed",
				"kind", string(kind),
				"retryable", retryable,
				"email", mask.EmailLabel(to))
			return
		}
		s.log.Info("password reset mail delivered",
			"event", "password_reset.delivered",
			"email", mask.EmailLabel(to))
	}()
}

// resetLink 拼出前端重置页的绝对链接。
// 令牌是 base64url，QueryEscape 只做防御性编码。
func (s *Service) resetLink(token string) string {
	return s.mailBase + "/reset-password?token=" + url.QueryEscape(token)
}

// Close 守在途投递结束，最多等到 ctx 结束。
// 服务器停机时调用；超时不 panic，残留 goroutine 由进程退出收尾。
func (s *Service) Close(ctx context.Context) error {
	done := make(chan struct{})
	go func() {
		s.mailWG.Wait()
		close(done)
	}()
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
