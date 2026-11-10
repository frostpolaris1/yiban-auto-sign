package mail_test

// SMTP 适配器的行为测试。用 mailtest 的进程内服务器，不连真实 SMTP。
//
// # 功能
// 钉住五条性质：
//   1. STARTTLS 与隐式 TLS 两档都能投递，且信封、正文、认证用户正确；
//   2. 失败按可重试/不可重试分类，日志面只暴露粗分类；
//   3. 口令不出现在错误文本里；
//   4. 配置错误在构造期失败，不把畸形地址带进 SMTP 会话；
//   5. 地址可表达性的边界：注册接受且域名是主机名形状的地址可投递，
//      其余注册接受的地址被显性拒收并带 core/mail.ErrUnsupportedAddress。
// 本文件**不**测真实服务器的证书链、SPF/DKIM 或投递回执。
//
// # 口径
// 所有等待有上界；不读系统时钟造证书（见 mailtest.selfSigned）。

import (
	"context"
	"crypto/tls"
	"errors"
	"net"
	"strings"
	"testing"
	"time"

	mailadapter "github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/mail"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/mail/mailtest"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coremail "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mail"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
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

// TestSendAcceptsSenderWithDisplayName 钉住带显示名的发件人配置。
//
// 真实部署会写 YIBAN_MAIL_FROM=易班 <no-reply@example.test>。
// 显示名是非 ASCII 文本，但不进入信封，也不进入本实现发出的头部：
// 只有 addr-spec 生效。此前整串扫非 ASCII，这类配置让启动直接失败。
// 断言用户可见结果：配置被接受、投递成功、信封发件人是 addr-spec。
func TestSendAcceptsSenderWithDisplayName(t *testing.T) {
	srv, err := mailtest.Start(mailtest.StartTLS)
	if err != nil {
		t.Fatalf("start fake smtp: %v", err)
	}
	t.Cleanup(srv.Close)
	srv.SetCredentials(testUser, testPass)

	cfg := configFor(t, srv, "starttls")
	cfg.From = "易班 <no-reply@example.test>"
	sender, err := mailadapter.NewSMTP(cfg)
	if err != nil {
		t.Fatalf("NewSMTP 拒绝了带显示名的发件人: %v", err)
	}
	if err := sender.Send(t.Context(), coremail.Message{
		To: "user@example.test", Subject: "重置口令", Text: "body",
	}); err != nil {
		t.Fatalf("Send: %v", err)
	}
	if !srv.WaitMessages(1, 3*time.Second) {
		t.Fatal("服务器没有收到邮件")
	}
	got := srv.Messages()[0]
	if got.From != "no-reply@example.test" {
		t.Fatalf("MAIL FROM = %q, want %q（信封只用 addr-spec）", got.From, "no-reply@example.test")
	}
	if !strings.Contains(got.Data, "From: <no-reply@example.test>") {
		t.Fatalf("From 头不是 addr-spec 形态：\n%s", got.Data)
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

// TestSendAcceptsRegistrationAddressesThatFailStrictParsing 钉住宽松回退分支。
//
// 五个样本都通过 core/user.ValidEmail（注册口径），
// 都被 net/mail.ParseAddress 拒绝（严格口径）。
// 它们的域名只含主机名字符，故回退分支必须接收：
// 只按严格口径判，这些用户能注册却永远收不到重置信。
// 反例见 TestSendRejectsAddressThatBreaksEnvelopeFraming。
// 断言用户可见结果：信真的到了服务器，信封里的地址被引用成安全形态。
func TestSendAcceptsRegistrationAddressesThatFailStrictParsing(t *testing.T) {
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
	cases := []struct {
		to       string
		wantRcpt string
	}{
		{"a,b@example.com", `"a,b"@example.com`},
		{"a;b@example.com", `"a;b"@example.com`},
		{"a<b>@example.com", `"a<b>"@example.com`},
		{".ab@example.com", `".ab"@example.com`},
		{"a@e.", "a@e."},
	}
	for i, tc := range cases {
		if !coreuser.ValidEmail(tc.to) {
			t.Fatalf("样本 %q 不在注册口径内，本测试失去判别力", tc.to)
		}
		err := sender.Send(t.Context(), coremail.Message{
			To: tc.to, Subject: "s", Text: "body",
		})
		if err != nil {
			t.Fatalf("Send(%q) 被本地拒绝: %v", tc.to, err)
		}
		if !srv.WaitMessages(i+1, 3*time.Second) {
			t.Fatalf("服务器没有收到寄往 %q 的邮件", tc.to)
		}
		got := srv.Messages()[i]
		if len(got.To) != 1 || got.To[0] != tc.wantRcpt {
			t.Fatalf("RCPT TO = %v, want [%s]（地址 %q）", got.To, tc.wantRcpt, tc.to)
		}
		if !strings.Contains(got.Data, "To: <"+tc.wantRcpt+">") {
			t.Fatalf("To 头与信封不一致（地址 %q）：\n%s", tc.to, got.Data)
		}
	}
}

// TestSendRejectsAddressThatBreaksEnvelopeFraming 钉住宽松口径的边界。
//
// 域名里的 > 会提前闭合 RCPT TO 的尖括号，改变命令结构。
// 注册口径不管这件事（ValidEmail 只查 @ 形状），投递口径必须管。
// 这类地址属于「注册接受、投递不能表达」，错误链必须带
// core/mail.ErrUnsupportedAddress：日志靠它记 reason=unsupported_address。
func TestSendRejectsAddressThatBreaksEnvelopeFraming(t *testing.T) {
	sender, err := mailadapter.NewSMTP(mailadapter.Config{
		Host: "127.0.0.1", Port: 465, Username: testUser, Password: testPass,
		From: testUser, TLSMode: "auto",
	})
	if err != nil {
		t.Fatalf("NewSMTP: %v", err)
	}
	const to = "user@example.test>,b"
	if !coreuser.ValidEmail(to) {
		t.Fatalf("样本 %q 不在注册口径内，本测试失去判别力", to)
	}
	err = sender.Send(context.Background(), coremail.Message{To: to, Subject: "s", Text: "body"})
	if err == nil {
		t.Fatal("Send 接受了能把分隔符带出尖括号的地址")
	}
	if kind, retryable := coremail.Classify(err); kind != coremail.KindConfig || retryable {
		t.Fatalf("分类 = (%s, retryable=%t), want (config, false)", kind, retryable)
	}
	if !errors.Is(err, coremail.ErrUnsupportedAddress) {
		t.Fatalf("注册接受但投递拒收的地址没有 ErrUnsupportedAddress，日志会缺 reason: %v", err)
	}

	// 反例：不在注册口径内的畸形地址不带该原因。
	// 否则「注册接受但投递拒收」这一条日志原因失去判别力。
	err = sender.Send(context.Background(), coremail.Message{
		To: "not-an-address", Subject: "s", Text: "body",
	})
	if err == nil {
		t.Fatal("Send 接受了畸形地址")
	}
	if errors.Is(err, coremail.ErrUnsupportedAddress) {
		t.Fatalf("畸形地址不该带 ErrUnsupportedAddress: %v", err)
	}
}

// TestSendRejectsAddressesTheDeliveryPathCannotExpress 钉住投递侧的显性拒收。
//
// 四个样本都通过注册口径（ValidEmail），本投递路径都无法表达：
//  1. 含控制字符 0x01/0x7F：net/mail 引用时会把它丢掉
//     （实测 a\x01b@example.com 变成 "ab"@example.com），
//     投递地址与注册地址不同，收件人不是注册人。
//  2. 含非 ASCII：本实现不发 SMTPUTF8，出站必被服务端拒。
//
// 断言用户可见结果：本地拒收（未连服务器）、分类 config、不可重试，
// 且错误链可判 core/mail.ErrUnsupportedAddress（日志按它记原因）。
func TestSendRejectsAddressesTheDeliveryPathCannotExpress(t *testing.T) {
	sender, err := mailadapter.NewSMTP(mailadapter.Config{
		Host: "127.0.0.1", Port: 465, Username: testUser, Password: testPass,
		From: testUser, TLSMode: "auto",
	})
	if err != nil {
		t.Fatalf("NewSMTP: %v", err)
	}
	cases := []struct{ name, to string }{
		{"control char 0x01", "a\x01b@example.com"},
		{"delete char 0x7f", "a\x7fb@example.com"},
		{"non-ASCII local part", "é@example.com"},
		{"non-ASCII domain", "a@例子.example"},
	}
	for _, tc := range cases {
		if !coreuser.ValidEmail(tc.to) {
			t.Fatalf("样本 %q 不在注册口径内，本测试失去判别力", tc.to)
		}
		err := sender.Send(context.Background(), coremail.Message{
			To: tc.to, Subject: "s", Text: "body",
		})
		if err == nil {
			t.Fatalf("%s: Send 接受了投递路径无法表达的地址 %q", tc.name, tc.to)
		}
		if kind, retryable := coremail.Classify(err); kind != coremail.KindConfig || retryable {
			t.Fatalf("%s: 分类 = (%s, retryable=%t), want (config, false); err=%v",
				tc.name, kind, retryable, err)
		}
		if !errors.Is(err, coremail.ErrUnsupportedAddress) {
			t.Fatalf("%s: 错误链没有 core/mail.ErrUnsupportedAddress: %v", tc.name, err)
		}
	}
}

// TestEveryDeclaredTLSModeIsAccepted 是配置闭集与解析器的对照断言。
//
// 词表只在 core/mail 定义。往那里加词却忘了改解析器，
// 启动期就会接受一个永远无法投递的模式，本测试在此红。
func TestEveryDeclaredTLSModeIsAccepted(t *testing.T) {
	for _, mode := range coremail.TLSModes() {
		cfg := mailadapter.Config{
			Host: "127.0.0.1", Port: 465, Username: testUser, Password: testPass,
			From: testUser, TLSMode: mode,
		}
		if _, err := mailadapter.NewSMTP(cfg); err != nil {
			t.Fatalf("闭集里的模式 %q 适配器不认识: %v", mode, err)
		}
	}
	if got := len(coremail.TLSModes()); got == 0 {
		t.Fatal("TLS 模式闭集为空——扫空不得当绿")
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

// TestSendStartTLSMissingIsConfigClassified 钉住「服务器不提供 STARTTLS」的分类。
//
// starttls 模式的语义是强制升级：服务器不公告 STARTTLS 属于端口/模式
// 配错，是 config 类失败，不可重试。旧实现与现实现都是本地判定：
// 适配器在显式问候后先查 STARTTLS 扩展公告（smtp.go Send 里的
// Extension 检查），缺失即返回 config 类错误，不依赖 go-mail 的
// 错误文本，也不会随 go-mail 升级漂移。
func TestSendStartTLSMissingIsConfigClassified(t *testing.T) {
	srv, err := mailtest.Start(mailtest.StartTLS)
	if err != nil {
		t.Fatalf("start fake smtp: %v", err)
	}
	t.Cleanup(srv.Close)
	srv.SetSuppressSTARTTLS(true)

	sender, err := mailadapter.NewSMTP(configFor(t, srv, "starttls"))
	if err != nil {
		t.Fatalf("NewSMTP: %v", err)
	}
	err = sender.Send(t.Context(), coremail.Message{
		To: "user@example.test", Subject: "s", Text: "body",
	})
	if err == nil {
		t.Fatal("服务器不提供 STARTTLS 时 Send 应当失败")
	}
	if kind, retryable := coremail.Classify(err); kind != coremail.KindConfig || retryable {
		t.Fatalf("分类 = (%s, retryable=%t), want (config, false); err=%v", kind, retryable, err)
	}
	if srv.MessageCount() != 0 {
		t.Fatal("未升级 TLS 仍然投递了邮件")
	}
}

// TestSendDeliversRawUTF8BodyWith8BitCTE 钉住正文线上形状。
//
// 旧实现以 8bit 投递原始 UTF-8 正文。go-mail 的默认编码是
// quoted-printable：会把链接里的 `=` 转义成 =3D，破坏
// 「正文含完整链接」的判据。适配器选择 NoEncoding（CTE 8bit）保持
// 正文原样出线；本测试钉住 CTE 头、非 ASCII 正文与含等号的链接
// 都不被转义。
func TestSendDeliversRawUTF8BodyWith8BitCTE(t *testing.T) {
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
	link := "https://app.example.test/reset-password?token=a=b"
	if err := sender.Send(t.Context(), coremail.Message{
		To: "user@example.test", Subject: "重置口令",
		Text: "打开以下链接重置口令：\n" + link + "\n",
	}); err != nil {
		t.Fatalf("Send: %v", err)
	}
	if !srv.WaitMessages(1, 3*time.Second) {
		t.Fatal("服务器没有收到邮件")
	}
	got := srv.Messages()[0]
	if !strings.Contains(got.Data, "Content-Transfer-Encoding: 8bit") {
		t.Fatalf("正文编码不是 8bit：\n%s", got.Data)
	}
	if !strings.Contains(got.Data, link) {
		t.Fatalf("链接被编码破坏（应原样出现）：\n%s", got.Data)
	}
	if !strings.Contains(got.Data, "打开以下链接重置口令：") {
		t.Fatalf("非 ASCII 正文应原样出线：\n%s", got.Data)
	}
}

// TestSendImplicitTLSDeliversOnNonLoopbackServerName 钉住隐式 TLS 下的认证前提。
//
// go-mail 靠 conn.(*tls.Conn) 判定连接已加密；判不出时 PlainAuth 拒发凭据，
// 只有服务器名恰为 localhost/127.0.0.1/::1 时豁免。本测试用大写 LOCALHOST：
// 解析与证书校验大小写不敏感（仍命中 mailtest 的 localhost 证书），
// 但不在该豁免名单里——旧手写实现在这类主机名上一直能正常投递，
// 本用例复现旧行为：隐式 TLS＋认证必须在非豁免服务器名上成功。
func TestSendImplicitTLSDeliversOnNonLoopbackServerName(t *testing.T) {
	srv, err := mailtest.Start(mailtest.ImplicitTLS)
	if err != nil {
		t.Fatalf("start fake smtp: %v", err)
	}
	t.Cleanup(srv.Close)
	srv.SetCredentials(testUser, testPass)

	cfg := configFor(t, srv, "implicit")
	cfg.Host = "LOCALHOST"
	sender, err := mailadapter.NewSMTP(cfg)
	if err != nil {
		t.Fatalf("NewSMTP: %v", err)
	}
	if err := sender.Send(t.Context(), coremail.Message{
		To: "user@example.test", Subject: "s", Text: "body",
	}); err != nil {
		t.Fatalf("Send 在非豁免服务器名上被拒（凭据没有随隐式 TLS 发出）: %v", err)
	}
	if !srv.WaitMessages(1, 3*time.Second) {
		t.Fatal("服务器没有收到邮件")
	}
	if got := srv.Messages()[0].AuthUser; got != testUser {
		t.Fatalf("AUTH 用户 = %q, want %q", got, testUser)
	}
}

// TestSendWithout8BitMIMEExtensionIsConfigClassified 登记一个已知的收严：
//
// 旧实现不查 8BITMIME，硬发 CTE 8bit；go-mail 要求服务器公告该扩展，
// 未公告时本地拒绝（ErrNoUnencoded）。本仓决策：保留 8bit 正文
// （QP 会转义重置链接里的 `=`），接受这一收严，分类为 config、
// 不可重试——改服务器或改模式之前重试没有意义。
func TestSendWithout8BitMIMEExtensionIsConfigClassified(t *testing.T) {
	srv, err := mailtest.Start(mailtest.StartTLS)
	if err != nil {
		t.Fatalf("start fake smtp: %v", err)
	}
	t.Cleanup(srv.Close)
	srv.SetCredentials(testUser, testPass)
	srv.SetSuppress8BitMIME(true)

	sender, err := mailadapter.NewSMTP(configFor(t, srv, "starttls"))
	if err != nil {
		t.Fatalf("NewSMTP: %v", err)
	}
	err = sender.Send(t.Context(), coremail.Message{
		To: "user@example.test", Subject: "s", Text: "body",
	})
	if err == nil {
		t.Fatal("服务器不公告 8BITMIME 时 Send 应当失败")
	}
	if kind, retryable := coremail.Classify(err); kind != coremail.KindConfig || retryable {
		t.Fatalf("分类 = (%s, retryable=%t), want (config, false); err=%v", kind, retryable, err)
	}
	if srv.MessageCount() != 0 {
		t.Fatal("未发 MAIL FROM 前被拒，不应有邮件入库")
	}
}

// TestSendStartTLSRefusedIsConfigClassified 钉住升级被 5xx 拒绝的分类。
//
// 服务器对 STARTTLS 命令回 550：与「不公告 STARTTLS」同因——端口/模式
// 配错，改配置之前重试没有意义。这是旧实现 phaseTLS 的判据，
// 适配器重写后必须逐字保留（分类 config、不可重试）。
func TestSendStartTLSRefusedIsConfigClassified(t *testing.T) {
	srv, err := mailtest.Start(mailtest.StartTLS)
	if err != nil {
		t.Fatalf("start fake smtp: %v", err)
	}
	t.Cleanup(srv.Close)
	srv.SetRejectSTARTTLS(true)

	sender, err := mailadapter.NewSMTP(configFor(t, srv, "starttls"))
	if err != nil {
		t.Fatalf("NewSMTP: %v", err)
	}
	err = sender.Send(t.Context(), coremail.Message{
		To: "user@example.test", Subject: "s", Text: "body",
	})
	if err == nil {
		t.Fatal("STARTTLS 被拒时 Send 应当失败")
	}
	if kind, retryable := coremail.Classify(err); kind != coremail.KindConfig || retryable {
		t.Fatalf("分类 = (%s, retryable=%t), want (config, false); err=%v", kind, retryable, err)
	}
	if srv.MessageCount() != 0 {
		t.Fatal("未升级 TLS 仍然投递了邮件")
	}
}

// TestSendDialBlackholeReturnsWithinBudget 钉住拨号阶段受总预算约束。
//
// 生产调用方传 context.Background()（app/auth/mail.go），若拨号只信
// ctx，黑洞主机（SYN 无应答）要等 OS 的 connect 超时——Linux 约 127s、
// Windows 约 21s——足以占满全部投递槽触发 delivery_queue_full。
// 203.0.113.9 是 RFC 5737 TEST-NET-3，按规范不可路由；个别网络会把它
// 拦在本地（立即回 refusal/unreachable，或透明代理代答 TCP 握手——
// 实测有的本机代理只代答 25 端口，故用 587）。
// 本用例只对「拨号挂起」的网络形状有判别力：立即失败的形状会显式
// t.Skip（不静默通过，SKIP 不是绿）；只有 timeout 分类算通过。
// 断言两条：Send 在预算（750ms）加裕量内返回——上界远低于两端 OS
// 超时，挂到 OS 超时必红；错误分类为 timeout 且可重试。
func TestSendDialBlackholeReturnsWithinBudget(t *testing.T) {
	sender, err := mailadapter.NewSMTP(mailadapter.Config{
		Host: "203.0.113.9", Port: 587, Username: testUser, Password: testPass,
		From: testUser, TLSMode: "starttls", Timeout: 750 * time.Millisecond,
	})
	if err != nil {
		t.Fatalf("NewSMTP: %v", err)
	}
	start := clock.System{}.Now()
	err = sender.Send(context.Background(), coremail.Message{
		To: "user@example.test", Subject: "s", Text: "body",
	})
	if elapsed := time.Since(start); elapsed > 5*time.Second {
		t.Fatalf("Send 用时 %v：拨号不受预算约束，挂到了 OS 超时", elapsed)
	}
	if err == nil {
		t.Fatal("拨到不可路由地址应当失败（本网络把 TEST-NET-3 路由出去了？）")
	}
	kind, retryable := coremail.Classify(err)
	if kind == coremail.KindConnect {
		// 立即 refusal/unreachable：拨号没有经历挂起，本用例无法区分
		// 「预算生效」与「OS 秒回」。显式跳过而不是静默通过。
		t.Skipf("网络对 TEST-NET-3 立即失败（%v）；本用例需要黑洞路由才有判别力", err)
	}
	if kind != coremail.KindTimeout || !retryable {
		t.Fatalf("黑洞拨号分类 = (%s, retryable=%t), want (timeout, true); err=%v",
			kind, retryable, err)
	}
}

// TestSendSuppressesSMTPUTF8EvenWhenAdvertised 钉住 SMTPUTF8 参数抑制。
//
// 5a40a011 首版适配显式关闭该参数；换 vendored smtp 客户端时抑制一度
// 丢失：服务器公告 SMTPUTF8 时 MAIL FROM 就会带上参数。mailtest 的
// EHLO 常驻公告 SMTPUTF8，本测试断言线上 MAIL FROM 不含它——非 ASCII
// 地址已在本地拒收，参数只会增加与既有服务器对话的变量。
// 先断言前置条件（服务器真的公告了 SMTPUTF8），再断言结论：公告被
// 删时本用例显式变红，而不是空转通过。
func TestSendSuppressesSMTPUTF8EvenWhenAdvertised(t *testing.T) {
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
	if err := sender.Send(t.Context(), coremail.Message{
		To: "user@example.test", Subject: "s", Text: "body",
	}); err != nil {
		t.Fatalf("Send: %v", err)
	}
	if !srv.WaitMessages(1, 3*time.Second) {
		t.Fatal("服务器没有收到邮件")
	}
	// 前置条件：没有公告就没有约束对象，本用例失去判别力。
	if !srv.Advertises("SMTPUTF8") {
		t.Fatal("mailtest 的 EHLO 没有公告 SMTPUTF8，本用例失去判别力")
	}
	got := srv.Messages()[0]
	if strings.Contains(strings.ToUpper(got.MailFromLine), "SMTPUTF8") {
		t.Fatalf("MAIL FROM 带了 SMTPUTF8 参数: %q", got.MailFromLine)
	}
	if got.From != testUser {
		t.Fatalf("信封发件人 = %q, want %q（扩展参数不得破坏信封解析）", got.From, testUser)
	}
}

// settingsFor 构造一次解析结果，指向给定假服务器。
func settingsFor(srv *mailtest.Server) mailadapter.Settings {
	return mailadapter.Settings{
		Host:     "127.0.0.1",
		Port:     srv.Port(),
		Username: testUser,
		Password: testPass,
		From:     testUser,
		TLSMode:  "starttls",
	}
}

// 每发送解析：Resolve 返回的 host/port 在下一次 Send 生效，无需重建发送器。
// 两个进程内服务器各收到一封；第一封不会落到第二台。
func TestSMTPResolvePerSendUsesNextSettings(t *testing.T) {
	first, err := mailtest.Start(mailtest.StartTLS)
	if err != nil {
		t.Fatalf("start first fake smtp: %v", err)
	}
	t.Cleanup(first.Close)
	second, err := mailtest.Start(mailtest.StartTLS)
	if err != nil {
		t.Fatalf("start second fake smtp: %v", err)
	}
	t.Cleanup(second.Close)
	first.SetCredentials(testUser, testPass)
	second.SetCredentials(testUser, testPass)

	current := settingsFor(first)
	sender, err := mailadapter.NewSMTP(mailadapter.Config{
		Resolve: func(context.Context) (mailadapter.Settings, error) {
			return current, nil
		},
		// 两台进程内服务器各自自签，测试用跳过校验共享信任；
		// 生产 TLSConfig 恒为 nil，不提供该开关。
		TLSConfig: &tls.Config{InsecureSkipVerify: true}, //nolint:gosec // 进程内自签测试服务器
		Timeout:   3 * time.Second,
	})
	if err != nil {
		t.Fatalf("NewSMTP: %v", err)
	}
	msg := coremail.Message{To: "user@example.test", Subject: "per-send", Text: "body"}
	if err := sender.Send(context.Background(), msg); err != nil {
		t.Fatalf("first send: %v", err)
	}
	if !first.WaitMessages(1, 3*time.Second) {
		t.Fatal("first server did not receive the first message")
	}
	if second.MessageCount() != 0 {
		t.Fatalf("second server received %d messages before the switch", second.MessageCount())
	}

	current = settingsFor(second)
	if err := sender.Send(context.Background(), msg); err != nil {
		t.Fatalf("second send: %v", err)
	}
	if !second.WaitMessages(1, 3*time.Second) {
		t.Fatal("second server did not receive the message after the switch")
	}
	if first.MessageCount() != 1 {
		t.Fatalf("first server received %d messages, want 1", first.MessageCount())
	}
}

// Resolve 失败按连接类可重试返回，且不拨号；错误文本不含口令。
func TestSMTPResolveFailureIsRetryableWithoutDial(t *testing.T) {
	sender, err := mailadapter.NewSMTP(mailadapter.Config{
		Resolve: func(context.Context) (mailadapter.Settings, error) {
			return mailadapter.Settings{}, errors.New("settings unavailable")
		},
		Timeout: 3 * time.Second,
	})
	if err != nil {
		t.Fatalf("NewSMTP: %v", err)
	}
	err = sender.Send(context.Background(), coremail.Message{
		To: "user@example.test", Subject: "x", Text: "y",
	})
	if err == nil {
		t.Fatal("Send succeeded with a failing resolver")
	}
	if kind, retryable := coremail.Classify(err); kind != coremail.KindConnect || !retryable {
		t.Fatalf("classify = (%v, %v), want connect/retryable", kind, retryable)
	}
	if strings.Contains(err.Error(), testPass) {
		t.Fatal("error text contains the SMTP password")
	}
}

// 动态配置非法（空主机/缺口令对）同样在发送前拒绝，不拨号。
func TestSMTPResolveInvalidSettingsRejected(t *testing.T) {
	for _, tc := range []struct {
		name     string
		settings mailadapter.Settings
	}{
		{"empty host", mailadapter.Settings{Port: 587, Username: testUser, Password: testPass, From: testUser}},
		{"port out of range", mailadapter.Settings{Host: "127.0.0.1", Port: 0, Username: testUser, Password: testPass, From: testUser}},
		{"user without pass", mailadapter.Settings{Host: "127.0.0.1", Port: 587, Username: testUser, From: testUser}},
		{"unknown tls mode", mailadapter.Settings{Host: "127.0.0.1", Port: 587, Username: testUser, Password: testPass, From: testUser, TLSMode: "ssl"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			sender, err := mailadapter.NewSMTP(mailadapter.Config{
				Resolve: func(context.Context) (mailadapter.Settings, error) {
					return tc.settings, nil
				},
				Timeout: 3 * time.Second,
			})
			if err != nil {
				t.Fatalf("NewSMTP: %v", err)
			}
			err = sender.Send(context.Background(), coremail.Message{
				To: "user@example.test", Subject: "x", Text: "y",
			})
			if err == nil {
				t.Fatal("Send succeeded with invalid dynamic settings")
			}
			if kind, retryable := coremail.Classify(err); kind != coremail.KindConfig || retryable {
				t.Fatalf("classify = (%v, %v), want config/non-retryable", kind, retryable)
			}
		})
	}
}
