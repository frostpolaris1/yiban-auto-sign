package main

// SMTP 投递的端到端测试。真库、真 HTTP、真 SMTP 对话（进程内假服务器）。
//
// # 功能
// 模拟真实用户走完「忘记口令 → 收邮件 → 点链接 → 设新口令 → 登录」。
// 另钉三条失败语义：投递失败不改变 204；未配置 SMTP 留下显性记录；
// 同一邮箱连续请求被限速。
// 本文件**不**连真实 SMTP 服务商，**不**测证书链信任（假服务器自签根证书）。
//
// # 归属
// cmd/server 是唯一可同时引用 adapters、app、entry 的位置。
//
// # 口径
// 缺 DSN 时整包跳过（由 TestMain 与 newAuthEnv 处理）。
// 所有等待有上界；投递在后台进行，测试等服务器收到邮件后才断言。

import (
	"bytes"
	"log/slog"
	"net"
	"net/http"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/mail/mailtest"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
)

const (
	e2eMailUser = "mailer@example.test"
	e2eMailPass = "smtp-secret-e2e-9f21"
	e2eBaseURL  = "https://app.example.test"
)

var e2eTokenInLink = regexp.MustCompile(`reset-password\?token=([A-Za-z0-9_-]+)`)

// syncLog 是并发安全的日志缓冲。
type syncLog struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncLog) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncLog) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

func newCapturedLogger() (*syncLog, *slog.Logger) {
	logs := &syncLog{}
	return logs, slog.New(slog.NewTextHandler(logs, nil))
}

func waitForLog(t *testing.T, logs *syncLog, substr string) {
	t.Helper()
	deadline := clock.System{}.Now().Add(5 * time.Second)
	for (clock.System{}).Now().Before(deadline) {
		if strings.Contains(logs.String(), substr) {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("等待日志 %q 超时；当前日志：\n%s", substr, logs.String())
}

// mailEnvConfig 把装配配置指向假 SMTP 服务器。
func mailEnvConfig(port int) func(*config.Config) {
	return func(cfg *config.Config) {
		cfg.ResetMailEnabled = true
		cfg.MailHost = "127.0.0.1"
		cfg.MailPort = port
		cfg.MailUser = e2eMailUser
		cfg.MailPass = e2eMailPass
		cfg.MailFrom = e2eMailUser
		cfg.MailTLSMode = "starttls"
		cfg.PublicBaseURL = e2eBaseURL
	}
}

// 模拟真实用户：忘记口令后从邮件里拿到链接，用链接里的令牌设新口令，再登录。
// 断言用户可见结果：忘记口令 204；邮件链接可重置；新口令能登录，旧口令不能。
func TestSMTPForgotPasswordDeliversWorkingResetLink(t *testing.T) {
	srv, err := mailtest.Start(mailtest.StartTLS)
	if err != nil {
		t.Fatalf("start fake smtp: %v", err)
	}
	t.Cleanup(srv.Close)
	srv.SetCredentials(e2eMailUser, e2eMailPass)

	logs, logger := newCapturedLogger()
	e := newAuthEnvWith(t, func(cfg *config.Config) {
		mailEnvConfig(srv.Port())(cfg)
		cfg.MailTLSConfig = srv.TLSConfig()
	}, logger)
	e.createUser("ivy@example.com", "old-horse")

	resp, payload := e.do(http.MethodPost, "/api/auth/forgot-password",
		map[string]string{"email": "ivy@example.com"}, nil)
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("forgot status = %d, want 204; body=%q", resp.StatusCode, payload)
	}
	if !srv.WaitMessages(1, 5*time.Second) {
		t.Fatalf("SMTP 服务器没有收到邮件；日志：\n%s", logs.String())
	}
	data := srv.Messages()[0].Data
	m := e2eTokenInLink.FindStringSubmatch(data)
	if m == nil {
		t.Fatalf("邮件正文没有重置链接：\n%s", data)
	}
	token := m[1]
	if !strings.Contains(data, e2eBaseURL+"/reset-password?token="+token) {
		t.Fatalf("链接基址不是配置值：\n%s", data)
	}
	if strings.Contains(logs.String(), token) {
		t.Fatal("日志里出现了重置令牌")
	}
	if strings.Contains(logs.String(), e2eMailPass) {
		t.Fatal("日志里出现了 SMTP 口令")
	}
	if strings.Contains(logs.String(), e2eMailUser) {
		t.Fatal("日志里出现了 SMTP 用户名（sensitive=true）")
	}

	// 用户点链接后的动作：提交新口令。
	resp, payload = e.do(http.MethodPost, "/api/auth/reset-password",
		map[string]string{"token": token, "new_password": "new-horse"}, nil)
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("reset status = %d, want 204; body=%q", resp.StatusCode, payload)
	}
	if oldResp, _ := e.login("ivy@example.com", "old-horse"); oldResp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("旧口令登录 = %d, want 401", oldResp.StatusCode)
	}
	if newResp, body := e.login("ivy@example.com", "new-horse"); newResp.StatusCode != http.StatusOK {
		t.Fatalf("新口令登录 = %d, want 200; body=%+v", newResp.StatusCode, body)
	}
}

// 模拟真实部署：YIBAN_MAIL_FROM 写成「显示名 <地址>」。
// 断言用户可见结果：服务启动成功，重置邮件照常送达，信封发件人是 addr-spec。
func TestSMTPForgotPasswordDeliversWithDisplayNameSender(t *testing.T) {
	srv, err := mailtest.Start(mailtest.StartTLS)
	if err != nil {
		t.Fatalf("start fake smtp: %v", err)
	}
	t.Cleanup(srv.Close)
	srv.SetCredentials(e2eMailUser, e2eMailPass)

	logs, logger := newCapturedLogger()
	e := newAuthEnvWith(t, func(cfg *config.Config) {
		mailEnvConfig(srv.Port())(cfg)
		cfg.MailFrom = "易班 <no-reply@example.test>"
		cfg.MailTLSConfig = srv.TLSConfig()
	}, logger)
	e.createUser("nina@example.com", "correct-horse")

	resp, payload := e.do(http.MethodPost, "/api/auth/forgot-password",
		map[string]string{"email": "nina@example.com"}, nil)
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("forgot status = %d, want 204; body=%q", resp.StatusCode, payload)
	}
	if !srv.WaitMessages(1, 5*time.Second) {
		t.Fatalf("SMTP 服务器没有收到邮件；日志：\n%s", logs.String())
	}
	if got := srv.Messages()[0].From; got != "no-reply@example.test" {
		t.Fatalf("MAIL FROM = %q, want %q（信封只用 addr-spec）", got, "no-reply@example.test")
	}
}

// TestSMTPForgotPasswordFailureKeeps204 钉住投递失败不改响应的语义：
// 仍回 204，失败必须在日志里可见。
func TestSMTPForgotPasswordFailureKeeps204(t *testing.T) {
	// 先监听再关闭：连接被拒的端口，不依赖外部网络。
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	_, port, err := mailtest.HostPort(ln.Addr().String())
	_ = ln.Close()
	if err != nil {
		t.Fatalf("HostPort: %v", err)
	}
	logs, logger := newCapturedLogger()
	e := newAuthEnvWith(t, mailEnvConfig(port), logger)
	e.createUser("jane@example.com", "correct-horse")

	resp, payload := e.do(http.MethodPost, "/api/auth/forgot-password",
		map[string]string{"email": "jane@example.com"}, nil)
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("投递失败时 forgot status = %d, want 204; body=%q", resp.StatusCode, payload)
	}
	waitForLog(t, logs, "password_reset.delivery_failed")
	if !strings.Contains(logs.String(), "kind=connect") {
		t.Fatalf("失败日志缺少粗分类 connect：\n%s", logs.String())
	}
	if got := e.scalarInt("SELECT count(*) FROM password_resets"); got != 1 {
		t.Fatalf("投递失败时令牌行 = %d, want 1", got)
	}
	if strings.Contains(logs.String(), "token=") {
		t.Fatal("失败日志里出现了令牌形状的文本")
	}
}

// 未配置 SMTP：仍回 204，但必须留下显性「令牌已生成但未投递」记录。
func TestSMTPNotConfiguredLogsUndelivered(t *testing.T) {
	logs, logger := newCapturedLogger()
	e := newAuthEnvWith(t, nil, logger)
	e.createUser("karl@example.com", "correct-horse")

	resp, payload := e.do(http.MethodPost, "/api/auth/forgot-password",
		map[string]string{"email": "karl@example.com"}, nil)
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("forgot status = %d, want 204; body=%q", resp.StatusCode, payload)
	}
	waitForLog(t, logs, "password_reset.not_delivered")
	if !strings.Contains(logs.String(), "smtp_not_configured") {
		t.Fatalf("未投递记录缺少原因：\n%s", logs.String())
	}
	if strings.Contains(logs.String(), "token=") {
		t.Fatal("未投递记录里出现了令牌形状的文本")
	}
}

// 同一邮箱连续请求被限速：只产生一个令牌、一封邮件，第二次仍 204。
func TestSMTPForgotPasswordThrottlesRepeatedEmail(t *testing.T) {
	srv, err := mailtest.Start(mailtest.StartTLS)
	if err != nil {
		t.Fatalf("start fake smtp: %v", err)
	}
	t.Cleanup(srv.Close)
	srv.SetCredentials(e2eMailUser, e2eMailPass)

	logs, logger := newCapturedLogger()
	e := newAuthEnvWith(t, func(cfg *config.Config) {
		mailEnvConfig(srv.Port())(cfg)
		cfg.MailTLSConfig = srv.TLSConfig()
	}, logger)
	e.createUser("liam@example.com", "correct-horse")

	for i := 0; i < 2; i++ {
		resp, payload := e.do(http.MethodPost, "/api/auth/forgot-password",
			map[string]string{"email": "liam@example.com"}, nil)
		if resp.StatusCode != http.StatusNoContent {
			t.Fatalf("第 %d 次 forgot status = %d, want 204; body=%q", i+1, resp.StatusCode, payload)
		}
	}
	waitForLog(t, logs, "password_reset.throttled")
	if !srv.WaitMessages(1, 5*time.Second) {
		t.Fatal("SMTP 服务器没有收到邮件")
	}
	// 给第二封（若有）一点时间落地，再数。
	time.Sleep(200 * time.Millisecond)
	if got := srv.MessageCount(); got != 1 {
		t.Fatalf("限速窗口内投递了 %d 封邮件, want 1", got)
	}
	if got := e.scalarInt("SELECT count(*) FROM password_resets"); got != 1 {
		t.Fatalf("限速窗口内令牌行 = %d, want 1", got)
	}
}
