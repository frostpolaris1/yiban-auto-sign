// Package mail 是 core/mail.Sender 的 SMTP 实现。
//
// # 功能
// 用隐式 TLS（465 风格）或 STARTTLS（587 风格）投递纯文本邮件。
// 失败按 core/mail.Kind 分类，并标记可重试性。
// 本包**不**读环境变量、**不**记日志、**不**重试、**不**持久化。
//
// # 归属
// adapters 层设施包。装配代码把 config 的值注入 Config；
// 用例层只依赖 core/mail.Sender。
//
// # 复用
// Config、SMTP、NewSMTP、DefaultTimeout；错误分类词表在 core/mail。
//
// # 口径
// 两种 TLS 都**校验证书**；没有"跳过校验"开关。
// 一次 Send 的全部阶段共用一个绝对截止时间（默认 10s）：
// 连接、TLS、认证、DATA 都不单独续期。
// 失败分类：连接/TLS/超时可重试；认证、配置、DATA 之后的失败不可重试
// （DATA 可能已被接收，重试会造成双发）。
// 凭据只出现在 AUTH 命令里，不进错误文本、不进日志。
package mail

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"mime"
	"net"
	"net/mail"
	"net/smtp"
	"net/textproto"
	"strings"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coremail "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mail"
)

// DefaultTimeout 是一次 Send 的总预算。
// 取 10s：覆盖一次正常 SMTP 对话，又不会让后台投递长期占住槽位。
const DefaultTimeout = 10 * time.Second

// TLS 模式取值。与 config/registry.json 的 YIBAN_MAIL_TLS_MODE 同词表。
const (
	TLSModeAuto     = "auto"
	TLSModeImplicit = "implicit"
	TLSModeStartTLS = "starttls"
)

// Config 是 SMTP 投递的配置。全部字段由装配代码注入。
type Config struct {
	// Host 是 SMTP 服务器主机名。证书校验按它取 ServerName。
	Host string
	// Port 是 SMTP 端口。465 在 auto 模式下走隐式 TLS。
	Port int
	// Username 与 Password 是 AUTH PLAIN 凭据。两者同时为空表示不做认证。
	Username string
	Password string
	// From 是信封与头部发件地址。空时用 Username。
	From string
	// TLSMode 取 auto、implicit、starttls。auto 按端口选择。
	TLSMode string
	// Timeout 是一次投递的总预算。零值用 DefaultTimeout。
	Timeout time.Duration
	// TLSConfig 覆盖默认 TLS 参数。生产为 nil；测试注入信任根证书。
	// ServerName 为空时按 Host 填。
	TLSConfig *tls.Config
	// Clock 提供 Date 头与截止时间的时基。零值用系统时钟。
	Clock clock.Clock
}

// SMTP 实现 core/mail.Sender。
type SMTP struct {
	host     string
	addr     string
	user     string
	pass     string
	from     string
	implicit bool
	timeout  time.Duration
	tlsConf  *tls.Config
	clock    clock.Clock
}

// NewSMTP 校验配置并构造发送器。非法配置在启动期失败。
func NewSMTP(cfg Config) (*SMTP, error) {
	host := strings.TrimSpace(cfg.Host)
	if host == "" {
		return nil, errors.New("mail: SMTP host is required")
	}
	if cfg.Port < 1 || cfg.Port > 65535 {
		return nil, fmt.Errorf("mail: SMTP port %d is out of range 1..65535", cfg.Port)
	}
	if (cfg.Username == "") != (cfg.Password == "") {
		return nil, errors.New("mail: SMTP username and password must be set together")
	}
	fromRaw := strings.TrimSpace(cfg.From)
	if fromRaw == "" {
		fromRaw = strings.TrimSpace(cfg.Username)
	}
	if fromRaw == "" {
		return nil, errors.New("mail: sender address is required (YIBAN_MAIL_FROM or YIBAN_MAIL_USER)")
	}
	from, err := parseAddress(fromRaw)
	if err != nil {
		return nil, fmt.Errorf("mail: invalid sender address: %w", err)
	}
	implicit, err := resolveTLSMode(cfg.TLSMode, cfg.Port)
	if err != nil {
		return nil, err
	}
	timeout := cfg.Timeout
	if timeout <= 0 {
		timeout = DefaultTimeout
	}
	clk := cfg.Clock
	if clk == nil {
		clk = clock.System{}
	}
	return &SMTP{
		host:     host,
		addr:     net.JoinHostPort(host, fmt.Sprintf("%d", cfg.Port)),
		user:     cfg.Username,
		pass:     cfg.Password,
		from:     from,
		implicit: implicit,
		timeout:  timeout,
		tlsConf:  cfg.TLSConfig,
		clock:    clk,
	}, nil
}

// resolveTLSMode 把模式词解析成两档。
// auto：465 走隐式 TLS，其余走 STARTTLS（与 README 的端口约定一致）。
func resolveTLSMode(raw string, port int) (implicit bool, err error) {
	mode := strings.ToLower(strings.TrimSpace(raw))
	switch mode {
	case "", TLSModeAuto:
		return port == 465, nil
	case TLSModeImplicit:
		return true, nil
	case TLSModeStartTLS:
		return false, nil
	default:
		return false, fmt.Errorf("mail: unknown TLS mode %q (want auto, implicit or starttls)", raw)
	}
}

// Send 投递一封邮件。失败返回 *coremail.DeliveryError。
func (s *SMTP) Send(ctx context.Context, msg coremail.Message) error {
	if err := ctx.Err(); err != nil {
		return deliveryError(coremail.KindTimeout, true, err)
	}
	to, body, err := s.encode(msg)
	if err != nil {
		return err
	}

	// 总预算：取配置超时与 ctx 截止时间中更早者。
	deadline := s.clock.Now().Add(s.timeout)
	if d, ok := ctx.Deadline(); ok && d.Before(deadline) {
		deadline = d
	}

	dialer := &net.Dialer{Timeout: s.timeout}
	conn, err := dialer.DialContext(ctx, "tcp", s.addr)
	if err != nil {
		return wrapPhase(phaseConnect, err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(deadline)

	if s.implicit {
		tlsConn := tls.Client(conn, s.clientTLSConfig())
		if err := tlsConn.HandshakeContext(ctx); err != nil {
			return wrapPhase(phaseTLS, err)
		}
		conn = tlsConn
	}

	c, err := smtp.NewClient(conn, s.host)
	if err != nil {
		return wrapPhase(phaseConnect, err)
	}
	defer c.Close()

	if !s.implicit {
		// 先显式问候：EHLO 失败按连接类分类，而不是误报"服务器不支持 STARTTLS"。
		if err := c.Hello("localhost"); err != nil {
			return wrapPhase(phaseConnect, err)
		}
		if ok, _ := c.Extension("STARTTLS"); !ok {
			return deliveryError(coremail.KindConfig, false,
				errors.New("mail: server does not advertise STARTTLS"))
		}
		if err := c.StartTLS(s.clientTLSConfig()); err != nil {
			return wrapPhase(phaseTLS, err)
		}
	}
	if s.user != "" {
		// 两档 TLS 都在此之前建立；自定义 Auth 不受 net/smtp 的
		// "明文连接拒绝 PLAIN" 判定影响（隐式 TLS 下 Client.TLS 仍为 false）。
		if err := c.Auth(plainAuth{user: s.user, pass: s.pass}); err != nil {
			return wrapPhase(phaseAuth, err)
		}
	}
	if err := c.Mail(s.from); err != nil {
		return wrapPhase(phaseSender, err)
	}
	if err := c.Rcpt(to); err != nil {
		return wrapPhase(phaseRecipient, err)
	}
	w, err := c.Data()
	if err != nil {
		return wrapPhase(phaseData, err)
	}
	if _, err := w.Write(body); err != nil {
		_ = w.Close()
		return wrapPhase(phaseData, err)
	}
	if err := w.Close(); err != nil {
		return wrapPhase(phaseData, err)
	}
	// QUIT 失败不改变送达结论：服务器已 ACK 的邮件不得记成失败。
	_ = c.Quit()
	return nil
}

// clientTLSConfig 返回本次连接使用的 TLS 配置。默认校验证书。
func (s *SMTP) clientTLSConfig() *tls.Config {
	cfg := &tls.Config{}
	if s.tlsConf != nil {
		cfg = s.tlsConf.Clone()
	}
	if cfg.ServerName == "" {
		cfg.ServerName = s.host
	}
	if cfg.MinVersion == 0 {
		cfg.MinVersion = tls.VersionTLS12
	}
	return cfg
}

// encode 校验地址与头部形状，返回信封收件人与完整报文。
func (s *SMTP) encode(msg coremail.Message) (string, []byte, error) {
	to, err := parseAddress(msg.To)
	if err != nil {
		return "", nil, err
	}
	if strings.ContainsAny(msg.Subject, "\r\n") {
		return "", nil, deliveryError(coremail.KindConfig, false,
			errors.New("mail: subject contains a line break"))
	}
	subject := mime.QEncoding.Encode("UTF-8", msg.Subject)
	var b strings.Builder
	fmt.Fprintf(&b, "From: <%s>\r\n", s.from)
	fmt.Fprintf(&b, "To: <%s>\r\n", to)
	fmt.Fprintf(&b, "Subject: %s\r\n", subject)
	fmt.Fprintf(&b, "Date: %s\r\n", s.clock.Now().UTC().Format(time.RFC1123Z))
	b.WriteString("MIME-Version: 1.0\r\n")
	b.WriteString("Content-Type: text/plain; charset=UTF-8\r\n")
	b.WriteString("Content-Transfer-Encoding: 8bit\r\n\r\n")
	b.WriteString(normalizeNewlines(msg.Text))
	return to, []byte(b.String()), nil
}

// parseAddress 解析并收紧地址：拒绝换行与控制字符。
func parseAddress(raw string) (string, error) {
	if strings.ContainsAny(raw, "\r\n") {
		return "", deliveryError(coremail.KindConfig, false,
			errors.New("mail: address contains a line break"))
	}
	addr, err := mail.ParseAddress(strings.TrimSpace(raw))
	if err != nil || addr.Address == "" {
		return "", deliveryError(coremail.KindConfig, false,
			errors.New("mail: address is not a valid mail address"))
	}
	return addr.Address, nil
}

// normalizeNewlines 把正文统一成 LF。smtp 的 dotWriter 负责补 CRLF 与点转义。
func normalizeNewlines(text string) string {
	text = strings.ReplaceAll(text, "\r\n", "\n")
	return strings.ReplaceAll(text, "\r", "\n")
}

// plainAuth 是最小 AUTH PLAIN 实现。
//
// net/smtp 的 PlainAuth 在隐式 TLS 下仍认为连接是明文
// （Client.TLS 只在 StartTLS 后置真），会拒绝发送凭据。
// 本实现只在调用方已建立 TLS 之后使用。
type plainAuth struct {
	user string
	pass string
}

func (a plainAuth) Start(*smtp.ServerInfo) (string, []byte, error) {
	return "PLAIN", []byte("\x00" + a.user + "\x00" + a.pass), nil
}

func (a plainAuth) Next([]byte, bool) ([]byte, error) {
	return nil, nil
}

// phase 是失败发生的会话阶段。分类按阶段与 SMTP 状态码共同决定。
type phase int

const (
	phaseConnect phase = iota
	phaseTLS
	phaseAuth
	phaseSender
	phaseRecipient
	phaseData
)

// deliveryError 构造分类错误。
func deliveryError(kind coremail.Kind, retryable bool, err error) *coremail.DeliveryError {
	return &coremail.DeliveryError{Kind: kind, Retryable: retryable, Err: err}
}

// wrapPhase 按阶段分类。
func wrapPhase(p phase, err error) error {
	if err == nil {
		return nil
	}
	kind, retryable := classifyPhase(p, err)
	return deliveryError(kind, retryable, err)
}

// classifyPhase 的判据顺序：取消/超时 → 证书 → 状态码 → 阶段缺省。
func classifyPhase(p phase, err error) (coremail.Kind, bool) {
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return coremail.KindTimeout, true
	}
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return coremail.KindTimeout, true
	}
	var certErr *tls.CertificateVerificationError
	if errors.As(err, &certErr) {
		return coremail.KindConfig, false
	}
	var unknownAuthority x509.UnknownAuthorityError
	if errors.As(err, &unknownAuthority) {
		return coremail.KindConfig, false
	}
	var hostnameErr x509.HostnameError
	if errors.As(err, &hostnameErr) {
		return coremail.KindConfig, false
	}

	code, hasCode := smtpCode(err)
	switch p {
	case phaseConnect:
		if hasCode && code/100 == 5 {
			return coremail.KindProtocol, false
		}
		return coremail.KindConnect, true
	case phaseTLS:
		if hasCode && code/100 == 5 {
			return coremail.KindConfig, false
		}
		return coremail.KindConnect, true
	case phaseAuth:
		return coremail.KindAuth, false
	case phaseSender:
		return coremail.KindSender, hasCode && code/100 == 4
	case phaseRecipient:
		return coremail.KindRecipient, hasCode && code/100 == 4
	case phaseData:
		// DATA 可能已被接收；重试有双发风险。
		return coremail.KindMessage, false
	default:
		return coremail.KindProtocol, false
	}
}

// smtpCode 取服务器应答码。net/smtp 用 *textproto.Error 承载它。
func smtpCode(err error) (int, bool) {
	var te *textproto.Error
	if errors.As(err, &te) {
		return te.Code, true
	}
	return 0, false
}
