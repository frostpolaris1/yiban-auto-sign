package auth_test

// 忘记口令的邮件接线测试。
//
// # 功能
// 用进程内替身钉住四条不变式：
//   1. 已知邮箱生成的重置链接里的令牌，与库里存的摘要匹配；
//   2. 投递失败不改变用例结果（HTTP 层仍是 204），且失败进日志；
//   3. SMTP 未配置时留下显性「未投递」记录，不静默；
//   4. 同一邮箱的连续请求被限速，不产生第二个令牌或第二封信。
// 本文件**不**连数据库，**不**发 HTTP（HTTP 由 cmd/server 的 e2e 钉）。
//
// # 口径
// 投递在后台进行；测试用 fake 的通道等待，超时有上界。
// 日志断言只看事件名与粗分类，**不看**完整日志文案。
// 令牌只许出现在邮件正文，任何日志行都不得含它。

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"

	appauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/auth"
	appuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/user"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coremail "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mail"
)

const resetBaseURL = "https://app.example.test"

// syncBuffer 是并发安全的日志缓冲：投递在后台 goroutine 里写日志。
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// fakeMail 记录投递请求并回报固定错误。
type fakeMail struct {
	mu     sync.Mutex
	msgs   []coremail.Message
	err    error
	signal chan struct{}
}

func newFakeMail() *fakeMail {
	return &fakeMail{signal: make(chan struct{}, 8)}
}

func (f *fakeMail) Send(_ context.Context, msg coremail.Message) error {
	f.mu.Lock()
	f.msgs = append(f.msgs, msg)
	err := f.err
	f.mu.Unlock()
	select {
	case f.signal <- struct{}{}:
	default:
	}
	return err
}

func (f *fakeMail) wait(t *testing.T) coremail.Message {
	t.Helper()
	select {
	case <-f.signal:
	case <-time.After(3 * time.Second):
		t.Fatal("等待投递超时")
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.msgs[len(f.msgs)-1]
}

func (f *fakeMail) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.msgs)
}

// mailHarness 是带邮件端口与日志缓冲的用例环境。
type mailHarness struct {
	*harness
	logs *syncBuffer
}

func newMailHarness(t *testing.T, sender coremail.Sender) *mailHarness {
	t.Helper()
	h := &harness{
		users:    newFakeUsers(),
		sessions: &fakeSessions{},
		resets:   &fakeResets{},
		audits:   &fakeAudits{},
	}
	logs := &syncBuffer{}
	svc, err := appauth.NewService(appauth.Deps{
		Users:         h.users,
		Sessions:      h.sessions,
		OtherSessions: h.sessions,
		Resets:        h.resets,
		Audits:        h.audits,
		Tx:            fakeTx{},
		Clock:         testClock,
		KDF:           coreauth.PlainHasher{},
		Mail:          sender,
		MailBaseURL:   resetBaseURL,
		Logger:        slog.New(slog.NewTextHandler(logs, nil)),
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	h.svc = svc
	return &mailHarness{harness: h, logs: logs}
}

// seedKnown 建一个已知用户。
func (h *mailHarness) seedKnown(email string) {
	h.users.byEmail[email] = appuser.Record{
		ID: "u-mail", Email: email, Role: coreauth.RoleUser, Status: coreauth.StatusActive,
	}
}

var tokenInLink = regexp.MustCompile(`reset-password\?token=([A-Za-z0-9_-]+)`)

// 模拟真实用户：忘记口令后从邮件里点链接。
// 断言用户可见结果：链接指向配置基址，令牌与库内摘要匹配。
func TestForgotPasswordDeliversWorkingLink(t *testing.T) {
	fake := newFakeMail()
	h := newMailHarness(t, fake)
	h.seedKnown("known@example.com")

	if err := h.svc.ForgotPassword(t.Context(), "Known@Example.com"); err != nil {
		t.Fatalf("ForgotPassword: %v", err)
	}
	msg := fake.wait(t)
	if msg.To != "known@example.com" {
		t.Fatalf("收件人 = %q, want 归一后的地址", msg.To)
	}
	if msg.Subject == "" {
		t.Fatal("邮件没有主题")
	}
	m := tokenInLink.FindStringSubmatch(msg.Text)
	if m == nil {
		t.Fatalf("邮件正文没有重置链接：\n%s", msg.Text)
	}
	if !strings.Contains(msg.Text, resetBaseURL+"/reset-password?token="+m[1]) {
		t.Fatalf("链接基址不是配置值：\n%s", msg.Text)
	}
	// 有效期文案必须出现在正文里；它的具体数字由 ResetTokenTTL 派生，
	// 派生口径由包内的 TestResetTTLTextFollowsTokenTTL 钉住。
	if !strings.Contains(msg.Text, "小时内有效") {
		t.Fatalf("正文缺有效期说明：\n%s", msg.Text)
	}
	if len(h.resets.created) != 1 {
		t.Fatalf("令牌行 = %d, want 1", len(h.resets.created))
	}
	if coreauth.HashToken(m[1]) != h.resets.created[0].TokenHash {
		t.Fatal("邮件里的令牌与库内摘要不匹配")
	}
}

// 投递失败不得改变用例结果；失败进日志；令牌不进日志。
func TestForgotPasswordDeliveryFailureDoesNotChangeResult(t *testing.T) {
	fake := newFakeMail()
	fake.err = &coremail.DeliveryError{
		Kind: coremail.KindAuth, Retryable: false, Err: errors.New("535 auth failed"),
	}
	h := newMailHarness(t, fake)
	h.seedKnown("known@example.com")

	if err := h.svc.ForgotPassword(t.Context(), "known@example.com"); err != nil {
		t.Fatalf("投递失败改变了用例结果: %v", err)
	}
	msg := fake.wait(t)
	m := tokenInLink.FindStringSubmatch(msg.Text)
	if m == nil {
		t.Fatal("邮件正文没有重置链接")
	}
	waitLog(t, h.logs, "password_reset.delivery_failed")
	logs := h.logs.String()
	if !strings.Contains(logs, "kind=auth") {
		t.Fatalf("失败日志没有粗分类: %s", logs)
	}
	if strings.Contains(logs, m[1]) {
		t.Fatal("日志里出现了重置令牌")
	}
}

// 投递侧无法表达的地址必须留下显性原因记录。
//
// 收件人来自数据库；含控制字符或非 ASCII 的地址在投递侧被显性拒收。
// 只有粗分类 config 不够：运维要能一眼区分「地址不可表达」与「凭据错」。
func TestForgotPasswordUnsupportedAddressIsLogged(t *testing.T) {
	fake := newFakeMail()
	fake.err = &coremail.DeliveryError{
		Kind: coremail.KindConfig, Retryable: false,
		Err: fmt.Errorf("mail: address contains a control character: %w",
			coremail.ErrUnsupportedAddress),
	}
	h := newMailHarness(t, fake)
	h.seedKnown("known@example.com")

	if err := h.svc.ForgotPassword(t.Context(), "known@example.com"); err != nil {
		t.Fatalf("投递失败改变了用例结果: %v", err)
	}
	waitLog(t, h.logs, "password_reset.delivery_failed")
	logs := h.logs.String()
	for _, want := range []string{
		"kind=config", "retryable=false", "reason=unsupported_address",
	} {
		if !strings.Contains(logs, want) {
			t.Fatalf("失败日志缺少 %q:\n%s", want, logs)
		}
	}
}

// SMTP 未配置：用例仍成功，但必须留一条显性「未投递」记录。
func TestForgotPasswordWithoutSMTPIsExplicit(t *testing.T) {
	h := newMailHarness(t, nil)
	h.seedKnown("known@example.com")

	if err := h.svc.ForgotPassword(t.Context(), "known@example.com"); err != nil {
		t.Fatalf("ForgotPassword: %v", err)
	}
	waitLog(t, h.logs, "password_reset.not_delivered")
	logs := h.logs.String()
	if !strings.Contains(logs, "smtp_not_configured") {
		t.Fatalf("未投递记录缺少原因: %s", logs)
	}
	if !strings.Contains(logs, "token created") {
		t.Fatalf("未投递记录没有说明令牌已生成: %s", logs)
	}
}

// 同一邮箱连续请求：只产生一个令牌、一封邮件；后续请求仍成功。
func TestForgotPasswordThrottlesRepeatedEmail(t *testing.T) {
	fake := newFakeMail()
	h := newMailHarness(t, fake)
	h.seedKnown("known@example.com")

	if err := h.svc.ForgotPassword(t.Context(), "known@example.com"); err != nil {
		t.Fatalf("first ForgotPassword: %v", err)
	}
	fake.wait(t)
	if err := h.svc.ForgotPassword(t.Context(), "known@example.com"); err != nil {
		t.Fatalf("second ForgotPassword: %v", err)
	}
	if err := h.svc.ForgotPassword(t.Context(), "known@example.com"); err != nil {
		t.Fatalf("third ForgotPassword: %v", err)
	}
	waitLog(t, h.logs, "password_reset.throttled")

	if got := len(h.resets.created); got != 1 {
		t.Fatalf("令牌行 = %d, want 1（限速窗口内）", got)
	}
	if got := fake.count(); got != 1 {
		t.Fatalf("投递次数 = %d, want 1（限速窗口内）", got)
	}
}

// panicMail 在投递时 panic，模拟实现缺陷。
type panicMail struct{}

func (panicMail) Send(context.Context, coremail.Message) error { panic("boom") }

// 投递实现 panic 不得拖垮进程，也不得改变用例结果。
func TestForgotPasswordDeliveryPanicIsContained(t *testing.T) {
	h := newMailHarness(t, panicMail{})
	h.seedKnown("known@example.com")

	if err := h.svc.ForgotPassword(t.Context(), "known@example.com"); err != nil {
		t.Fatalf("投递 panic 改变了用例结果: %v", err)
	}
	waitLog(t, h.logs, "password_reset.delivery_failed")
	// 记账用 core/mail 词表里的 panic 类，不是随手写的字面量。
	if want := "kind=" + string(coremail.KindPanic); !strings.Contains(h.logs.String(), want) {
		t.Fatalf("panic 未按词表记账（want %q）：\n%s", want, h.logs.String())
	}
	if strings.Contains(h.logs.String(), "boom") {
		t.Fatal("panic 值落进了日志")
	}
}

// 未知邮箱不建令牌、不发信、不留投递记录：没有可投递的对象。
func TestForgotPasswordUnknownEmailSendsNothing(t *testing.T) {
	fake := newFakeMail()
	h := newMailHarness(t, fake)

	if err := h.svc.ForgotPassword(t.Context(), "absent@example.com"); err != nil {
		t.Fatalf("ForgotPassword: %v", err)
	}
	if fake.count() != 0 {
		t.Fatalf("未知邮箱投递了 %d 封邮件", fake.count())
	}
	if len(h.resets.created) != 0 {
		t.Fatalf("未知邮箱建了 %d 个令牌", len(h.resets.created))
	}
	if logs := h.logs.String(); strings.Contains(logs, "password_reset.") {
		t.Fatalf("未知邮箱写入了投递日志: %s", logs)
	}
}

// waitLog 等到日志缓冲包含事件名或超时。
func waitLog(t *testing.T, logs *syncBuffer, event string) {
	t.Helper()
	deadline := clock.System{}.Now().Add(3 * time.Second)
	for (clock.System{}).Now().Before(deadline) {
		if strings.Contains(logs.String(), event) {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("等待日志事件 %q 超时；当前日志：\n%s", event, logs.String())
}

// 保证 io 被使用（NewService 的 Logger 缺省走 slog.Default，这里是显式注入的证明）。
var _ io.Writer = (*syncBuffer)(nil)
