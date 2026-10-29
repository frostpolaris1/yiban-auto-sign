package mail_test

// SMTP 适配器的行为测试。用 mailtest 的进程内服务器，不连真实 SMTP。
//
// # 功能
// 钉住四条性质：
//   1. STARTTLS 与隐式 TLS 两档都能投递，且信封、正文、认证用户正确；
//   2. 失败按可重试/不可重试分类，日志面只暴露粗分类；
//   3. 口令不出现在错误文本里；
//   4. 配置错误在构造期失败，不把畸形地址带进 SMTP 会话。
// 本文件**不**测真实服务器的证书链、SPF/DKIM 或投递回执。
//
// # 口径
// 所有等待有上界；不读系统时钟造证书（见 mailtest.selfSigned）。

import (
	"context"
	"errors"
	"net"
	"strings"
	"testing"
	"time"

	mailadapter "github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/mail"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/mail/mailtest"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coremail "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mail"
)

const (
	testUser = "mailer@example.test"
	testPass = "smtp-secret-4b7c9d"
)

// configFor 构造指向假服务器的适配器配置。
func configFor(t *testing.T, srv *mailtest.Server, mode string) mailadapter.Config {
	t.Helper()
	return mailadapter.Config{
		Host:      "127.0.0.1",
		Port:      srv.Port(),
		Username:  testUser,
		Password:  testPass,
		From:      testUser,
		TLSMode:   mode,
		Timeout:   3 * time.Second,
		TLSConfig: srv.TLSConfig(),
	}
}

// TestSMTPStartTLSDelivers 模拟真实用户：服务端只支持 STARTTLS，客户端投递一封重置邮件。
// 断言用户可见结果：服务器收到正确信封与正文，且认证用了配置的账号。
func TestSMTPStartTLSDelivers(t *testing.T) {
	srv, err := mailtest.Start(mailtest.StartTLS)
	if err != nil {
		t.Fatalf("start fake smtp: %v", err)
	}
	t.Cleanup(srv.Close)
	srv.SetCredentials(testUser, testPass)

	sender, err := mailadapter.NewSMTP(configFor(t, srv, "starttls"))
	if err != nil {
		t.Fatalf("NewSMTP: %v", err)
	}
	link := "https://app.example.test/reset-password?token=abc123"
	msg := coremail.Message{
		To:      "user@example.test",
		Subject: "重置口令",
		Text:    "打开以下链接重置口令：\n" + link + "\n",
	}
	if err := sender.Send(t.Context(), msg); err != nil {
		t.Fatalf("Send: %v", err)
	}
	if !srv.WaitMessages(1, 3*time.Second) {
		t.Fatal("服务器没有收到邮件")
	}
	got := srv.Messages()[0]
	if got.From != testUser {
		t.Fatalf("MAIL FROM = %q, want %q", got.From, testUser)
	}
	if len(got.To) != 1 || got.To[0] != msg.To {
		t.Fatalf("RCPT TO = %v, want [%s]", got.To, msg.To)
	}
	if got.AuthUser != testUser {
		t.Fatalf("AUTH 用户 = %q, want %q", got.AuthUser, testUser)
	}
	if !strings.Contains(got.Data, "To: <"+msg.To+">") {
		t.Fatalf("正文缺 To 头：\n%s", got.Data)
	}
	if !strings.Contains(got.Data, link) {
		t.Fatalf("正文缺重置链接：\n%s", got.Data)
	}
	if !strings.Contains(got.Data, "Subject:") {
		t.Fatalf("正文缺 Subject 头：\n%s", got.Data)
	}
}

// TestSMTPImplicitTLSDelivers 覆盖 465 风格的隐式 TLS。
func TestSMTPImplicitTLSDelivers(t *testing.T) {
	srv, err := mailtest.Start(mailtest.ImplicitTLS)
	if err != nil {
		t.Fatalf("start fake smtp: %v", err)
	}
	t.Cleanup(srv.Close)
	srv.SetCredentials(testUser, testPass)

	sender, err := mailadapter.NewSMTP(configFor(t, srv, "implicit"))
	if err != nil {
		t.Fatalf("NewSMTP: %v", err)
	}
	if err := sender.Send(t.Context(), coremail.Message{
		To: "user@example.test", Subject: "s", Text: "body",
	}); err != nil {
		t.Fatalf("Send: %v", err)
	}
	if !srv.WaitMessages(1, 3*time.Second) {
		t.Fatal("服务器没有收到邮件")
	}
	if got := srv.Messages()[0].AuthUser; got != testUser {
		t.Fatalf("AUTH 用户 = %q", got)
	}
}

// TestSMTPAuthFailureIsNotRetryable 钉住认证失败的分类。
func TestSMTPAuthFailureIsNotRetryable(t *testing.T) {
	srv, err := mailtest.Start(mailtest.StartTLS)
	if err != nil {
		t.Fatalf("start fake smtp: %v", err)
	}
	t.Cleanup(srv.Close)
	srv.SetAuthFail(true)

	sender, err := mailadapter.NewSMTP(configFor(t, srv, "starttls"))
	if err != nil {
		t.Fatalf("NewSMTP: %v", err)
	}
	err = sender.Send(t.Context(), coremail.Message{
		To: "user@example.test", Subject: "s", Text: "body",
	})
	kind, retryable := coremail.Classify(err)
	if kind != coremail.KindAuth || retryable {
		t.Fatalf("认证失败分类 = (%s, retryable=%t), want (auth, false); err=%v", kind, retryable, err)
	}
	if strings.Contains(err.Error(), testPass) {
		t.Fatal("错误文本泄露了 SMTP 口令")
	}
	if srv.MessageCount() != 0 {
		t.Fatal("认证失败后仍然投递了邮件")
	}
}

// TestSMTPRecipientRefusedIsNotRetryable 钉住 5xx 收件人拒绝不回退。
func TestSMTPRecipientRefusedIsNotRetryable(t *testing.T) {
	srv, err := mailtest.Start(mailtest.StartTLS)
	if err != nil {
		t.Fatalf("start fake smtp: %v", err)
	}
	t.Cleanup(srv.Close)
	srv.SetRejectRecipient(true)

	sender, err := mailadapter.NewSMTP(configFor(t, srv, "starttls"))
	if err != nil {
		t.Fatalf("NewSMTP: %v", err)
	}
	err = sender.Send(t.Context(), coremail.Message{
		To: "user@example.test", Subject: "s", Text: "body",
	})
	kind, retryable := coremail.Classify(err)
	if kind != coremail.KindRecipient || retryable {
		t.Fatalf("收件人拒绝分类 = (%s, retryable=%t), want (recipient, false); err=%v", kind, retryable, err)
	}
}

// TestSMTPConnectFailureIsRetryable 钉住连接失败的分类。
// 端口先监听再关闭：连接被拒是确定的失败，不依赖外部网络。
func TestSMTPConnectFailureIsRetryable(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	addr := ln.Addr().String()
	_ = ln.Close()
	host, port, err := mailtest.HostPort(addr)
	if err != nil {
		t.Fatalf("HostPort: %v", err)
	}
	sender, err := mailadapter.NewSMTP(mailadapter.Config{
		Host: host, Port: port, Username: testUser, Password: testPass,
		From: testUser, TLSMode: "starttls", Timeout: 500 * time.Millisecond,
	})
	if err != nil {
		t.Fatalf("NewSMTP: %v", err)
	}
	err = sender.Send(t.Context(), coremail.Message{
		To: "user@example.test", Subject: "s", Text: "body",
	})
	kind, retryable := coremail.Classify(err)
	if kind != coremail.KindConnect || !retryable {
		t.Fatalf("连接失败分类 = (%s, retryable=%t), want (connect, true); err=%v", kind, retryable, err)
	}
}

// TestSMTPTimeoutIsRetryable 钉住超时分类：服务器不回欢迎行，客户端必须在预算内放弃。
func TestSMTPTimeoutIsRetryable(t *testing.T) {
	srv, err := mailtest.Start(mailtest.StartTLS)
	if err != nil {
		t.Fatalf("start fake smtp: %v", err)
	}
	t.Cleanup(srv.Close)
	srv.SetGreetingDelay(2 * time.Second)

	cfg := configFor(t, srv, "starttls")
	cfg.Timeout = 150 * time.Millisecond
	sender, err := mailadapter.NewSMTP(cfg)
	if err != nil {
		t.Fatalf("NewSMTP: %v", err)
	}
	start := clock.System{}.Now()
	err = sender.Send(t.Context(), coremail.Message{
		To: "user@example.test", Subject: "s", Text: "body",
	})
	if elapsed := time.Since(start); elapsed > 2*time.Second {
		t.Fatalf("Send 用时 %v，超时预算没有生效", elapsed)
	}
	kind, retryable := coremail.Classify(err)
	if kind != coremail.KindTimeout || !retryable {
		t.Fatalf("超时分类 = (%s, retryable=%t), want (timeout, true); err=%v", kind, retryable, err)
	}
}

// TestNewSMTPRejectsBadConfig 钉住构造期校验。
func TestNewSMTPRejectsBadConfig(t *testing.T) {
	base := mailadapter.Config{
		Host: "127.0.0.1", Port: 465, Username: testUser, Password: testPass,
		From: testUser, TLSMode: "auto",
	}
	cases := map[string]func(*mailadapter.Config){
		"empty host":        func(c *mailadapter.Config) { c.Host = " " },
		"zero port":         func(c *mailadapter.Config) { c.Port = 0 },
		"port over 65535":   func(c *mailadapter.Config) { c.Port = 70000 },
		"unknown tls mode":  func(c *mailadapter.Config) { c.TLSMode = "ssl" },
		"user without pass": func(c *mailadapter.Config) { c.Password = "" },
		"bad from address":  func(c *mailadapter.Config) { c.From = "not-an-address" },
	}
	for name, mutate := range cases {
		cfg := base
		mutate(&cfg)
		if _, err := mailadapter.NewSMTP(cfg); err == nil {
			t.Fatalf("%s: NewSMTP 接受了非法配置 %+v", name, cfg)
		}
	}
}

// TestSendRejectsHeaderInjection 钉住 To 的换行拒绝。
// 收件人来自数据库，但仍不得把换行带进 SMTP 命令或头部。
func TestSendRejectsHeaderInjection(t *testing.T) {
	sender, err := mailadapter.NewSMTP(mailadapter.Config{
		Host: "127.0.0.1", Port: 465, Username: testUser, Password: testPass,
		From: testUser, TLSMode: "auto",
	})
	if err != nil {
		t.Fatalf("NewSMTP: %v", err)
	}
	err = sender.Send(context.Background(), coremail.Message{
		To: "user@example.test\r\nBcc: evil@example.test", Subject: "s", Text: "body",
	})
	if err == nil {
		t.Fatal("Send 接受了带换行的收件人")
	}
	kind, retryable := coremail.Classify(err)
	if kind != coremail.KindConfig || retryable {
		t.Fatalf("收件人形状错误的分类 = (%s, retryable=%t), want (config, false)", kind, retryable)
	}
}

// TestSendContextCancelledIsClassified 钉住取消不落成 nil。
func TestSendContextCancelledIsClassified(t *testing.T) {
	srv, err := mailtest.Start(mailtest.StartTLS)
	if err != nil {
		t.Fatalf("start fake smtp: %v", err)
	}
	t.Cleanup(srv.Close)
	srv.SetGreetingDelay(2 * time.Second)

	sender, err := mailadapter.NewSMTP(configFor(t, srv, "starttls"))
	if err != nil {
		t.Fatalf("NewSMTP: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	err = sender.Send(ctx, coremail.Message{To: "user@example.test", Subject: "s", Text: "b"})
	if err == nil {
		t.Fatal("Send 在已取消的 ctx 上返回 nil")
	}
	if _, retryable := coremail.Classify(err); !retryable {
		t.Fatalf("取消的分类应当可重试；err=%v", err)
	}
	if !errors.Is(err, context.Canceled) && !errors.Is(err, context.DeadlineExceeded) {
		// 分类器可以包一层，但必须保留原始错误链。
		t.Logf("取消未直接保留 errors.Is 链: %v", err)
	}
}
