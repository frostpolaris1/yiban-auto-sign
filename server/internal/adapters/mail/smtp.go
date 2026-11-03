// Package mail 是 core/mail.Sender 的 SMTP 实现。
//
// # 功能
// 用隐式 TLS（465 风格）或 STARTTLS（587 风格）投递纯文本邮件。
// SMTP 传输引擎是 wneessen/go-mail（版本锁 v0.8.1，MIT）：
// 连接、EHLO、STARTTLS、AUTH、MAIL/RCPT/DATA 由它驱动，本包只做
// 配置翻译、地址口径与失败分类，不手写 SMTP 对话。
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
// 两种 TLS 都**校验证书**；没有"跳过校验"开关。隐式 TLS 在本包的
// 拨号函数里握手（go-mail 的 useSSL 只在默认拨号路径生效）；
// STARTTLS 交由 go-mail 强制升级（TLSMandatory，未提供即失败）。
// 一次 Send 的全部阶段共用一个绝对截止时间（默认 10s）：
// 连接、TLS、认证、DATA 都不单独续期。go-mail 在会话中途把连接
// 截止时间重置为 connTimeout（checkConn→UpdateDeadline），本包用
// deadlineCapConn 把每次重设压回共享预算内。
// 失败分类：连接/TLS/超时可重试；认证、配置、DATA 之后的失败不可重试
// （DATA 可能已被接收，重试会造成双发）。分类判据顺序与旧实现一致：
// 取消/超时 → 证书 → STARTTLS 未提供 → SMTP 状态码 → 阶段缺省。
// 认证用 go-mail 的 PLAIN（凭据只走 TLS 连接）；发送阶段按
// go-mail SendError.Reason 映射回本包的阶段词表。
// 凭据只出现在 AUTH 命令里，不进错误文本、不进日志。
//
// **投递可接受集与注册可接受集的关系**：ASCII 地址里，投递不覆盖注册
// 接受的全部形态。保证可投递的形态：域名只含主机名字符（字母、数字、
// 点、连字符）。这类地址先走 net/mail 严格解析（严格侧通过即接受），
// 严格侧拒绝时按裸 addr-spec 接收（注册用 core/user.ValidEmail，较宽松）。
// 域名含 `>`、`,`、`;` 等主机名字符以外的字符时，分隔符会逃出尖括号：
// addrSpecSafe 拒收。该守卫是必需的注入守卫，不可删。
// 非 ASCII 与含控制字符的注册地址由投递侧显性拒收（SMTPUTF8 未实现）。
// 注册接受但投递拒收的地址记 reason=unsupported_address。
// 改动此处必须同时看 parseAddress 的注释与 smtp_test 的口径用例。
package mail

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"net"
	"net/mail"
	"net/textproto"
	"strings"
	"time"

	gomail "github.com/wneessen/go-mail"
	gmsmtp "github.com/wneessen/go-mail/smtp"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coremail "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mail"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// DefaultTimeout 是一次 Send 的总预算。
// 取 10s：覆盖一次正常 SMTP 对话，又不会让后台投递长期占住槽位。
const DefaultTimeout = 10 * time.Second

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
	// TLSMode 取 core/mail 的模式常量。auto 按端口选择。
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
	port     int
	user     string
	pass     string
	from     *mail.Address
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
		port:     cfg.Port,
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
// 词表住在 core/mail：本函数只解析，不定义闭集。
func resolveTLSMode(raw string, port int) (implicit bool, err error) {
	mode := strings.ToLower(strings.TrimSpace(raw))
	switch mode {
	case "", coremail.TLSModeAuto:
		return port == 465, nil
	case coremail.TLSModeImplicit:
		return true, nil
	case coremail.TLSModeStartTLS:
		return false, nil
	default:
		return false, fmt.Errorf("mail: unknown TLS mode %q (want %s)",
			raw, strings.Join(coremail.TLSModes(), ", "))
	}
}

// Send 投递一封邮件。失败返回 *coremail.DeliveryError。
//
// 会话分三段分类：拨号（连接、TLS、EHLO、STARTTLS）、认证、发送
// （MAIL/RCPT/DATA）。QUIT 失败不改变送达结论。
func (s *SMTP) Send(ctx context.Context, msg coremail.Message) error {
	if err := ctx.Err(); err != nil {
		return deliveryError(coremail.KindTimeout, true, err)
	}
	m, err := s.encode(msg)
	if err != nil {
		return err
	}

	// 总预算：取配置超时与 ctx 截止时间中更早者。
	deadline := s.clock.Now().Add(s.timeout)
	if d, ok := ctx.Deadline(); ok && d.Before(deadline) {
		deadline = d
	}

	c, err := s.newClient(deadline)
	if err != nil {
		return wrapConnect(err)
	}
	conn, err := c.DialToSMTPClientWithContext(ctx)
	if err != nil {
		return wrapConnect(err)
	}
	if s.user != "" {
		// 两档 TLS 都在此之前建立。allowUnenc=false：凭据只走 TLS 连接，
		// 与旧手写 plainAuth 的使用前提一致。
		if err := conn.Auth(gmsmtp.PlainAuth("", s.user, s.pass, s.host, false)); err != nil {
			return wrapAuth(err)
		}
	}
	if err := c.SendWithSMTPClient(conn, m); err != nil {
		return s.wrapSend(ctx, deadline, err)
	}
	// QUIT 失败不改变送达结论：服务器已 ACK 的邮件不得记成失败。
	_ = c.CloseWithSMTPClient(conn)
	return nil
}

// newClient 构造本次投递的 go-mail 客户端。
//
// 拨号函数注入共享截止时间（见 deadlineCapConn）；隐式 TLS 走 WithSSL
// 只为告诉 go-mail「连接已是 TLS」，握手在拨号函数里完成。
// WithoutNoop/WithoutRset：旧会话不含这两条命令，保持对话形状不变；
// WithoutRset 同时保证 DATA 被 ACK 之后不再有能改变结论的命令。
func (s *SMTP) newClient(deadline time.Time) (*gomail.Client, error) {
	opts := []gomail.Option{
		gomail.WithPort(s.port),
		gomail.WithHELO("localhost"),
		gomail.WithTimeout(s.timeout),
		gomail.WithTLSConfig(s.clientTLSConfig()),
		gomail.WithDialContextFunc(s.dial(deadline)),
		gomail.WithoutNoop(),
		gomail.WithoutRset(),
		// 旧实现从不发 SMTPUTF8；go-mail 只要服务器公告该扩展就会在
		// MAIL FROM 上附加参数。保持关：非 ASCII 地址已在本地拒收。
		gomail.WithoutSMTPUTF8(),
	}
	if s.implicit {
		opts = append(opts, gomail.WithSSL())
	} else {
		opts = append(opts, gomail.WithTLSPolicy(gomail.TLSMandatory))
	}
	return gomail.NewClient(s.host, opts...)
}

// dial 返回带截止时间上限的拨号函数。
// 隐式 TLS 在这里直接握手（等价旧实现的 tls.Client+HandshakeContext）；
// STARTTLS 先明文拨号，升级交给 go-mail 的 TLSMandatory 路径。
func (s *SMTP) dial(deadline time.Time) gomail.DialContextFunc {
	return func(ctx context.Context, network, addr string) (net.Conn, error) {
		var d net.Dialer
		var conn net.Conn
		var err error
		if s.implicit {
			conn, err = (&tls.Dialer{NetDialer: &d, Config: s.clientTLSConfig()}).DialContext(ctx, network, addr)
		} else {
			conn, err = d.DialContext(ctx, network, addr)
		}
		if err != nil {
			return nil, err
		}
		return deadlineCapConn{Conn: conn, budget: deadline}, nil
	}
}

// deadlineCapConn 把连接截止时间压在共享预算内。
//
// go-mail 在会话中途把截止时间重设为 connTimeout（checkConn 与拨号路径）；
// 不压住的话每个慢阶段都能各吃满一个预算，破坏「一次 Send 共用一个
// 绝对截止时间」的口径。重设请求超过预算时按预算封顶，更早的截止时间
// 原样保留（ctx 截止时间比配置预算早时仍然生效）。
type deadlineCapConn struct {
	net.Conn
	budget time.Time
}

func (c deadlineCapConn) SetDeadline(t time.Time) error {
	return c.Conn.SetDeadline(c.cap(t))
}

func (c deadlineCapConn) SetReadDeadline(t time.Time) error {
	return c.Conn.SetReadDeadline(c.cap(t))
}

func (c deadlineCapConn) SetWriteDeadline(t time.Time) error {
	return c.Conn.SetWriteDeadline(c.cap(t))
}

func (c deadlineCapConn) cap(t time.Time) time.Time {
	if t.After(c.budget) {
		return c.budget
	}
	return t
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

// encode 校验地址与头部形状，把 Message 翻译成 go-mail 报文。
//
// 报文形状与旧手写实现一致：信封与头部只用 addr-spec（显示名丢弃）、
// 主题 Q 编码（go-mail 对非 ASCII 头自动做）、正文 8bit 原样 UTF-8
// （NoEncoding；QP 会把链接里的 `=` 转义成 =3D，破坏"正文含完整链接"
// 的判据）。日期取注入时钟的 UTC 值。失败只来自本地形状检查。
func (s *SMTP) encode(msg coremail.Message) (*gomail.Msg, error) {
	to, err := parseAddress(msg.To)
	if err != nil {
		return nil, err
	}
	if strings.ContainsAny(msg.Subject, "\r\n") {
		return nil, deliveryError(coremail.KindConfig, false,
			errors.New("mail: subject contains a line break"))
	}
	m := gomail.NewMsg(gomail.WithEncoding(gomail.NoEncoding))
	m.SetDateWithValue(s.clock.Now().UTC())
	m.FromMailAddress(s.from)
	m.ToMailAddress(to)
	m.Subject(msg.Subject)
	m.SetBodyString(gomail.TypeTextPlain, normalizeNewlines(msg.Text))
	return m, nil
}

// parseAddress 解析地址并返回可直接放进信封与头部的形态。
//
// 判据顺序：
//  1. 换行一律拒绝：它们能把内容挤出 SMTP 命令与头部。
//  2. 控制字符（< 0x20 或 0x7F）一律拒绝，扫整串输入（见 controlCharReason）。
//  3. net/mail.ParseAddress 通过时，取其 addr-spec；该 addr-spec 含
//     非 ASCII 字符则拒绝（见 nonASCIIReason）。
//     显示名不进入信封与头部，故显示名里的非 ASCII 不受此限。
//  4. 否则 core/user.ValidEmail 通过时，按裸 addr-spec 接收；
//     先扫原文的非 ASCII 字符，再查域名形状（见 addrSpecSafe）。
//  5. 注册口径接受、以上两档都拒绝：记 core/mail.ErrUnsupportedAddress。
//  6. 两者都拒绝才报普通配置错误。
//
// 第 4 条覆盖严格解析拒绝的 ASCII 地址：注册口径只查 @ 形状，
// 投递口径查 RFC 5322。只按严格侧放行，会让已注册的地址收不到重置信。
// 覆盖范围有边：域名只含主机名字符时才保证可投递。
// 域名里的 `>`、`,`、`;` 等分隔符能逃出尖括号，addrSpecSafe 必须拒收；
// 该守卫是注入面，不可删。
// 第 5 条把「注册接受、投递不能表达」收成一个谓词：日志原因不按分支发散。
// 返回的 mail.Address 只带 addr-spec；头部与信封由 mail.Address.String()
// 渲染——它对特殊局部部分加引号（a,b@example.com 变 "a,b"@example.com），
// 与旧 quoteAddress 同一机制，故不在此预加引号。
func parseAddress(raw string) (*mail.Address, error) {
	if strings.ContainsAny(raw, "\r\n") {
		return nil, deliveryError(coremail.KindConfig, false,
			errors.New("mail: address contains a line break"))
	}
	s := strings.TrimSpace(raw)
	if err := controlCharReason(s); err != nil {
		return nil, deliveryError(coremail.KindConfig, false, err)
	}
	if addr, err := mail.ParseAddress(s); err == nil && addr.Address != "" {
		if err := nonASCIIReason(addr.Address); err != nil {
			return nil, deliveryError(coremail.KindConfig, false, err)
		}
		return &mail.Address{Address: addr.Address}, nil
	}
	if err := nonASCIIReason(s); err != nil {
		return nil, deliveryError(coremail.KindConfig, false, err)
	}
	if coreuser.ValidEmail(s) && addrSpecSafe(s) {
		return &mail.Address{Address: s}, nil
	}
	if coreuser.ValidEmail(s) {
		return nil, deliveryError(coremail.KindConfig, false,
			fmt.Errorf("mail: address cannot be put in the SMTP envelope: %w",
				coremail.ErrUnsupportedAddress))
	}
	return nil, deliveryError(coremail.KindConfig, false,
		errors.New("mail: address is not a valid mail address"))
}

// controlCharReason 报告控制字符（< 0x20 与 0x7F）。
//
// 扫整串输入而非解析结果：net/mail 引用时会丢弃控制字符
// （实测 a\x01b@example.com 变成 "ab"@example.com），
// 投递地址≠注册地址，收件人不是注册人。
// 显示名里的控制字符一并拒绝：头部由本实现拼接，从严没有代价。
// 调用方先去掉首尾空白：粘贴来的尾随制表符不该让启动失败。
// 错误文本只带码点，不带地址本身。
func controlCharReason(raw string) error {
	for _, r := range raw {
		if r < 0x20 || r == 0x7F {
			return fmt.Errorf("mail: address contains control character U+%04X: %w",
				r, coremail.ErrUnsupportedAddress)
		}
	}
	return nil
}

// nonASCIIReason 报告地址里的非 ASCII 字符。
//
// 本实现不发 SMTPUTF8，出站必被服务端拒，故在本地先拒。
// 只扫 addr-spec，**不**扫整串输入：显示名可以是非 ASCII，
// 它既不进信封也不进本实现发出的头部。
// 回退分支没有解析结果，调用方把原文传进来。
func nonASCIIReason(addrSpec string) error {
	for _, r := range addrSpec {
		if r >= 0x80 {
			return fmt.Errorf("mail: address contains non-ASCII character U+%04X (SMTPUTF8 not implemented): %w",
				r, coremail.ErrUnsupportedAddress)
		}
	}
	return nil
}

// addrSpecSafe 报告裸 addr-spec 能否安全放进 SMTP 命令。
//
// 局部部分由 mail.Address.String() 引用，分隔符不会逃出引号。
// 域名不经引用，故只接受字母、数字、点与连字符：
// 其余字符（如 <>(),;）会提前闭合尖括号，改变命令结构。
// 非 ASCII 已由 nonASCIIReason 在调用点拒掉：这里不再放行。
func addrSpecSafe(addrSpec string) bool {
	local, domain, ok := strings.Cut(addrSpec, "@")
	if !ok || local == "" || domain == "" {
		return false
	}
	for _, r := range domain {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9':
		case r == '.', r == '-':
		default:
			return false
		}
	}
	return true
}

// normalizeNewlines 把正文统一成 LF。smtp 的 dotWriter 负责补 CRLF 与点转义。
func normalizeNewlines(text string) string {
	text = strings.ReplaceAll(text, "\r\n", "\n")
	return strings.ReplaceAll(text, "\r", "\n")
}

// deliveryError 构造分类错误。
func deliveryError(kind coremail.Kind, retryable bool, err error) *coremail.DeliveryError {
	return &coremail.DeliveryError{Kind: kind, Retryable: retryable, Err: err}
}

// wrapConnect 分类拨号阶段的失败：连接、隐式 TLS 握手、EHLO、STARTTLS。
// 判据顺序与旧实现一致：取消/超时 → 证书 → STARTTLS 未提供 → 状态码 → 缺省。
// 「服务器不提供 STARTTLS」旧实现本地判定为 config；go-mail 把它报成
// 普通错误，这里按其稳定文本识别（版本锁 v0.8.1，升级时必须复核该文本）。
// 其余 5xx 应答视为协议错误：旧实现按发生点细分 protocol/config，
// 拨号阶段归并后统一不可重试，可重试性不变。
func wrapConnect(err error) error {
	if err == nil {
		return nil
	}
	kind, retryable := classifyConnect(err)
	return deliveryError(kind, retryable, err)
}

func classifyConnect(err error) (coremail.Kind, bool) {
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
	if strings.Contains(err.Error(), "does not support STARTTLS") {
		return coremail.KindConfig, false
	}
	if code, ok := smtpCode(err); ok && code/100 == 5 {
		return coremail.KindProtocol, false
	}
	return coremail.KindConnect, true
}

// wrapAuth 分类认证失败。
// TLS 已在拨号阶段建立，故除取消/超时外一律不可重试：
// 凭据错重试只会再失败。
func wrapAuth(err error) error {
	if err == nil {
		return nil
	}
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return deliveryError(coremail.KindTimeout, true, err)
	}
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return deliveryError(coremail.KindTimeout, true, err)
	}
	return deliveryError(coremail.KindAuth, false, err)
}

// wrapSend 分类发送阶段（MAIL/RCPT/DATA）的失败。
//
// 连接级截止时间到只暴露成读写超时，不一定带 ctx 错误，故先查
// ctx 与共享预算：预算耗尽按超时分类，与旧实现一致。
// go-mail 用 SendError.Reason 区分步骤，映射回本包词表：
// MAIL 4xx 可重试、RCPT 4xx 可重试、DATA 之后一律不可重试（双发风险）、
// 连接检查失败归连接类。无码即不可重试，与旧实现同判据。
func (s *SMTP) wrapSend(ctx context.Context, deadline time.Time, err error) error {
	if err == nil {
		return nil
	}
	if ctx.Err() != nil || s.clock.Now().After(deadline) {
		return deliveryError(coremail.KindTimeout, true, err)
	}
	var se *gomail.SendError
	if !errors.As(err, &se) {
		return deliveryError(coremail.KindProtocol, false, err)
	}
	switch se.Reason {
	case gomail.ErrConnCheck:
		return deliveryError(coremail.KindConnect, true, err)
	case gomail.ErrGetSender, gomail.ErrGetRcpts, gomail.ErrNoUnencoded:
		// 地址已过本包口径仍被拒，或服务器不支持 8bit：属配置/能力错。
		return deliveryError(coremail.KindConfig, false, err)
	case gomail.ErrSMTPMailFrom:
		return deliveryError(coremail.KindSender, se.ErrorCode()/100 == 4, err)
	case gomail.ErrSMTPRcptTo:
		return deliveryError(coremail.KindRecipient, se.ErrorCode()/100 == 4, err)
	case gomail.ErrSMTPData, gomail.ErrSMTPDataClose, gomail.ErrWriteContent:
		// DATA 可能已被接收；重试有双发风险。
		return deliveryError(coremail.KindMessage, false, err)
	default:
		return deliveryError(coremail.KindProtocol, false, err)
	}
}

// smtpCode 取服务器应答码。SMTP 会话错误用 *textproto.Error 承载它。
func smtpCode(err error) (int, bool) {
	var te *textproto.Error
	if errors.As(err, &te) {
		return te.Code, true
	}
	return 0, false
}
