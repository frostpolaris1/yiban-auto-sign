// Package mail 是 core/mail.Sender 的 SMTP 实现。
//
// # 功能
// 用隐式 TLS（465 风格）或 STARTTLS（587 风格）投递纯文本邮件。
// SMTP 传输引擎是 wneessen/go-mail（版本锁 v0.8.1，MIT）：
// 报文构造、问候、STARTTLS、AUTH、MAIL/RCPT/DATA 由它驱动，本包只做
// 配置翻译、地址口径、连接所有权与失败分类，不手写 SMTP 对话。
// 失败按 core/mail.Kind 分类，并标记可重试性。
// 本包**不**读环境变量、**不**记日志、**不**重试、**不**持久化。
//
// # 归属
// adapters 层设施包。装配代码把 config 的值注入 Config；
// 用例层只依赖 core/mail.Sender。
// 注入 Config.Resolve 后，Host/Port/凭据/发件地址在**每次 Send 前**
// 重新解析：设置页写回的下一次外呼即生效，无需重启。
// 信任根、超时与时基仍由构造期注入，不随解析变化。
//
// # 复用
// Config、Settings、SMTP、NewSMTP、DefaultTimeout；错误分类词表在 core/mail。
//
// # 口径
// 两种 TLS 都**校验证书**；没有"跳过校验"开关。隐式 TLS 在本包的
// dial 里握手：vendored smtp.NewClient 用 conn.(*tls.Conn) 判定连接
// 已加密，判不出时 PLAIN 认证只对 localhost/127.0.0.1/::1 豁免、
// 生产主机名一律拒发凭据，故返回给它的必须是裸 *tls.Conn
// （内部包着 deadlineCapConn，封顶不受影响）。STARTTLS 由本包决定：
// 先显式问候，再查扩展（缺失即 config，不猜），升级失败按状态码分类。
// 连接归一次 Send 所有：失败路径必关，成功路径在 QUIT 后幂等收尾。
// 一次 Send 的全部阶段共用一个绝对截止时间（默认 10s）：
// 连接、TLS、认证、DATA 都不单独续期。拨号也受同一预算约束（dial 里
// context.WithDeadline）：生产调用方传 context.Background()，只信 ctx
// 的话，黑洞主机要等 OS 的 SYN 超时（Linux 约 127s），足以占满全部
// 投递槽。go-mail 在会话中途把连接截止时间重置为 connTimeout
// （checkConn→UpdateDeadline），本包用 deadlineCapConn 把每次重设
// 压回共享预算内。
// 失败分类：连接/TLS/超时可重试；认证、配置、DATA 之后的失败不可重试
// （DATA 可能已被接收，重试会造成双发）。分类判据顺序与旧实现一致：
// 取消/超时 → 证书 → SMTP 状态码 → 阶段缺省。
// 认证用 go-mail 的 PLAIN（allowUnenc=false：凭据只走 TLS 连接）；
// 发送阶段按 go-mail SendError.Reason 映射回本包的阶段词表。
// 正文保持 CTE 8bit 原样 UTF-8（NoEncoding）：QP 会把链接里的 `=`
// 转义成 =3D，破坏"正文含完整链接"的判据。已知收严：go-mail 要求
// 服务器公告 8BITMIME 才发 8bit 正文（旧实现硬发），未公告时本地拒绝，
// 分类 config、不可重试（smtp_test 有显式用例钉住该分类）。
// 凭据只出现在 AUTH 命令里，不进错误文本、不进日志。
//
// **投递可接受集与注册可接受集的关系**：ASCII 地址里，投递不覆盖注册
// 接受的全部形态。保证可投递的形态：域名只含主机名字符（字母、数字、
// 点、连字符）。这类地址先走 net/mail 严格解析（严格侧通过即接受），
// 严格侧拒绝时按裸 addr-spec 接收（注册用 core/user.ValidEmail，较宽松）。
// 域名含 `>`、`,`、`;` 等主机名字符以外的字符时，分隔符会逃出尖括号：
// addrSpecSafe 拒收。该守卫是必需的注入守卫，不可删。
// 非 ASCII 与含控制字符的注册地址由投递侧显性拒收（SMTPUTF8 参数
// 在 Send 里主动抑制，见 SkipSMTPUTF8）。
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
	"strconv"
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
	// Resolve 非 nil 时，每次 Send 前调用它取当前配置：
	// 上面的 Host/Port/Username/Password/From/TLSMode 被忽略，
	// 下一次外呼即用解析结果（设置页写回热生效）。
	// TLSConfig/Timeout/Clock 仍取本结构，**不**随解析变化：
	// 部署面不提供按发送切换信任根或超时的开关。
	// Resolve 为 nil 时行为与旧版逐位一致（构造期定型）。
	Resolve func(ctx context.Context) (Settings, error)
	// Timeout 是一次投递的总预算。零值用 DefaultTimeout。
	Timeout time.Duration
	// TLSConfig 覆盖默认 TLS 参数。生产为 nil；测试注入信任根证书。
	// ServerName 为空时按 Host 填。
	TLSConfig *tls.Config
	// Clock 提供 Date 头与截止时间的时基。零值用系统时钟。
	Clock clock.Clock
}

// Settings 是每次发送前解析的动态配置子集。
// 它只含会随设置页写回变化的字段；信任根、超时与时基不在此列。
type Settings struct {
	// Host 是 SMTP 服务器主机名。
	Host string
	// Port 是 SMTP 端口。
	Port int
	// Username 与 Password 是 AUTH PLAIN 凭据。两者同时为空表示不做认证。
	Username string
	Password string
	// From 是信封与头部发件地址。空时用 Username。
	From string
	// TLSMode 取 core/mail 的模式常量。
	TLSMode string
}

// resolvedSettings 是校验后的发送参数。
type resolvedSettings struct {
	host     string
	port     int
	user     string
	pass     string
	from     *mail.Address
	implicit bool
}

// SMTP 实现 core/mail.Sender。
type SMTP struct {
	static  resolvedSettings
	resolve func(ctx context.Context) (Settings, error)
	timeout time.Duration
	tlsConf *tls.Config
	clock   clock.Clock
}

// NewSMTP 校验配置并构造发送器。非法静态配置在启动期失败；
// 注入 Resolve 时动态配置在每次 Send 前校验，失败按配置类错误返回，
// 不拨号、不外呼。
func NewSMTP(cfg Config) (*SMTP, error) {
	timeout := cfg.Timeout
	if timeout <= 0 {
		timeout = DefaultTimeout
	}
	clk := cfg.Clock
	if clk == nil {
		clk = clock.System{}
	}
	sender := &SMTP{
		resolve: cfg.Resolve,
		timeout: timeout,
		tlsConf: cfg.TLSConfig,
		clock:   clk,
	}
	if cfg.Resolve == nil {
		resolved, err := resolveSettings(Settings{
			Host:     cfg.Host,
			Port:     cfg.Port,
			Username: cfg.Username,
			Password: cfg.Password,
			From:     cfg.From,
			TLSMode:  cfg.TLSMode,
		})
		if err != nil {
			return nil, err
		}
		sender.static = resolved
	}
	return sender, nil
}

// resolveSettings 校验一组发送参数并定型。
// 判据与旧 NewSMTP 逐条一致：主机非空、端口 1..65535、
// 用户名与口令同时给或同时空、发件地址可表达、TLS 档在词表内。
func resolveSettings(cfg Settings) (resolvedSettings, error) {
	host := strings.TrimSpace(cfg.Host)
	if host == "" {
		return resolvedSettings{}, errors.New("mail: SMTP host is required")
	}
	if cfg.Port < 1 || cfg.Port > 65535 {
		return resolvedSettings{}, fmt.Errorf("mail: SMTP port %d is out of range 1..65535", cfg.Port)
	}
	if (cfg.Username == "") != (cfg.Password == "") {
		return resolvedSettings{}, errors.New("mail: SMTP username and password must be set together")
	}
	fromRaw := strings.TrimSpace(cfg.From)
	if fromRaw == "" {
		fromRaw = strings.TrimSpace(cfg.Username)
	}
	if fromRaw == "" {
		return resolvedSettings{}, errors.New("mail: sender address is required (YIBAN_MAIL_FROM or YIBAN_MAIL_USER)")
	}
	from, err := parseAddress(fromRaw)
	if err != nil {
		return resolvedSettings{}, fmt.Errorf("mail: invalid sender address: %w", err)
	}
	implicit, err := resolveTLSMode(cfg.TLSMode, cfg.Port)
	if err != nil {
		return resolvedSettings{}, err
	}
	return resolvedSettings{
		host:     host,
		port:     cfg.Port,
		user:     cfg.Username,
		pass:     cfg.Password,
		from:     from,
		implicit: implicit,
	}, nil
}

// settings 返回本次发送使用的参数。
// Resolve 为 nil 时返回构造期定型的静态值（行为不变）；
// 非 nil 时每次调用解析并校验，错误按配置类包装，不拨号。
func (s *SMTP) settings(ctx context.Context) (resolvedSettings, error) {
	if s.resolve == nil {
		return s.static, nil
	}
	cfg, err := s.resolve(ctx)
	if err != nil {
		return resolvedSettings{}, deliveryError(coremail.KindConfig, false,
			fmt.Errorf("mail: resolve SMTP settings: %w", err))
	}
	resolved, err := resolveSettings(cfg)
	if err != nil {
		return resolvedSettings{}, deliveryError(coremail.KindConfig, false, err)
	}
	return resolved, nil
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
// 会话分四段分类：拨号（连接、隐式 TLS 握手、220 问候、EHLO）、
// STARTTLS 升级、认证、发送（MAIL/RCPT/DATA）。
// QUIT 失败不改变送达结论。
func (s *SMTP) Send(ctx context.Context, msg coremail.Message) error {
	if err := ctx.Err(); err != nil {
		return deliveryError(coremail.KindTimeout, true, err)
	}
	settings, err := s.settings(ctx)
	if err != nil {
		return err
	}
	m, err := s.encode(msg, settings)
	if err != nil {
		return err
	}

	// 总预算：取配置超时与 ctx 截止时间中更早者。
	deadline := s.clock.Now().Add(s.timeout)
	if d, ok := ctx.Deadline(); ok && d.Before(deadline) {
		deadline = d
	}

	c, err := gomail.NewClient(settings.host,
		gomail.WithTimeout(s.timeout),
		gomail.WithoutNoop(),
		gomail.WithoutRset(),
	)
	if err != nil {
		return wrapConnect(err)
	}

	conn, err := s.dial(ctx, deadline, settings)
	if err != nil {
		return wrapConnect(err)
	}
	// 连接归本次调用所有：任何失败路径都关掉，成功路径在 QUIT 之后
	// 幂等收尾。长期运行的后台投递不得靠 GC 回收 fd。
	defer func() { _ = conn.Close() }()

	client, err := gmsmtp.NewClient(conn, settings.host)
	if err != nil {
		return wrapConnect(err)
	}
	// 旧实现（含 5a40a011 首版适配）从不发 SMTPUTF8；go-mail 只要服务器
	// 公告该扩展就会在 MAIL FROM 上附加参数。保持抑制：非 ASCII 地址已在
	// 本地拒收，参数只会增加与既有服务器对话的变量。smtp_test 有用例
	// （mailtest 常驻公告 SMTPUTF8）钉住线上 MAIL FROM 不含该参数。
	client.SkipSMTPUTF8(true)
	if err := client.Hello("localhost"); err != nil {
		return wrapConnect(err)
	}
	if !settings.implicit {
		// 先显式问候（上方），再查 STARTTLS：问候失败按连接类分类，
		// 而不是误报"服务器不支持 STARTTLS"。
		if ok, _ := client.Extension("STARTTLS"); !ok {
			return deliveryError(coremail.KindConfig, false,
				errors.New("mail: server does not advertise STARTTLS"))
		}
		if err := client.StartTLS(s.clientTLSConfig(settings.host)); err != nil {
			return wrapStartTLS(err)
		}
	}
	if settings.user != "" {
		// 两档 TLS 都在此之前建立：隐式档由本包 dial 握手，STARTTLS 档
		// 由上一步升级成功。allowUnenc=false 与旧手写 plainAuth 同口径：
		// 凭据只走 TLS 连接，是否真的 TLS 由 vendored 客户端自行判定。
		if err := client.Auth(gmsmtp.PlainAuth("", settings.user, settings.pass, settings.host, false)); err != nil {
			return wrapAuth(err)
		}
	}
	if err := c.SendWithSMTPClient(client, m); err != nil {
		return s.wrapSend(ctx, deadline, err)
	}
	// QUIT 失败不改变送达结论：服务器已 ACK 的邮件不得记成失败。
	_ = c.CloseWithSMTPClient(client)
	return nil
}

// dial 建立到服务器的连接；隐式 TLS 在这里完成握手。
//
// 拨号与握手都套 Send 顶部算出的绝对预算（context.WithDeadline）：
// 生产调用方传 context.Background()，只信 ctx 的话，黑洞主机（SYN 无
// 应答）要等 OS 的 connect 超时——Linux 约 127s、Windows 约 21s——
// 足以占满全部投递槽（smtp_test 的黑洞用例钉住此事）。连接建立后
// 取消该 ctx 不影响已建立的连接（net 与 crypto/tls 的契约），故
// defer cancel 安全。
//
// 返回给 vendored smtp.NewClient 的连接必须能被它识别为 TLS：
// 它用 conn.(*tls.Conn) 判定，判不出时 PLAIN 认证只在 localhost/
// 127.0.0.1/::1 上放行，生产主机名一律拒发凭据（smtp_test 的
// 非豁免服务器名用例钉住此事）。因此隐式档返回裸 *tls.Conn，
// 但它在内部包着 deadlineCapConn，截止时间封顶仍然生效。
// STARTTLS 档先明文拨号，升级交给调用方的 StartTLS。
func (s *SMTP) dial(ctx context.Context, deadline time.Time, settings resolvedSettings) (net.Conn, error) {
	addr := net.JoinHostPort(settings.host, strconv.Itoa(settings.port))
	dialCtx, cancel := context.WithDeadline(ctx, deadline)
	defer cancel()
	raw, err := (&net.Dialer{}).DialContext(dialCtx, "tcp", addr)
	if err != nil {
		return nil, err
	}
	capped := deadlineCapConn{Conn: raw, budget: deadline}
	if err := capped.SetDeadline(deadline); err != nil {
		_ = raw.Close()
		return nil, err
	}
	if !settings.implicit {
		return capped, nil
	}
	tlsConn := tls.Client(capped, s.clientTLSConfig(settings.host))
	if err := tlsConn.HandshakeContext(dialCtx); err != nil {
		_ = raw.Close()
		return nil, err
	}
	return tlsConn, nil
}

// deadlineCapConn 把连接截止时间压在共享预算内。
//
// go-mail 在会话中途把截止时间重设为 connTimeout（checkConn 与拨号路径）；
// 不压住的话每个慢阶段都能各吃满一个预算，破坏「一次 Send 共用一个
// 绝对截止时间」的口径。重设请求超过预算时按预算封顶，更早的截止时间
// 原样保留（ctx 截止时间比配置预算早时仍然生效）。
// 注意：它只包 TCP 裸连接；*tls.Conn 直接包在它外面，
// 不存在「包装后类型断言失败」的问题。
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
func (s *SMTP) clientTLSConfig(host string) *tls.Config {
	cfg := &tls.Config{}
	if s.tlsConf != nil {
		cfg = s.tlsConf.Clone()
	}
	if cfg.ServerName == "" {
		cfg.ServerName = host
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
func (s *SMTP) encode(msg coremail.Message, settings resolvedSettings) (*gomail.Msg, error) {
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
	m.FromMailAddress(settings.from)
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

// wrapConnect 分类拨号阶段的失败：TCP 连接、隐式 TLS 握手、220 问候、
// EHLO。判据与旧实现一致：取消/超时 → 证书 → 5xx 视为协议错误 →
// 其余按连接类（可重试）。
func wrapConnect(err error) error {
	if err == nil {
		return nil
	}
	kind, retryable := classifyConnect(err)
	return deliveryError(kind, retryable, err)
}

func classifyConnect(err error) (coremail.Kind, bool) {
	if kind, retryable, ok := timeoutOrCert(err); ok {
		return kind, retryable
	}
	if code, ok := smtpCode(err); ok && code/100 == 5 {
		return coremail.KindProtocol, false
	}
	return coremail.KindConnect, true
}

// wrapStartTLS 分类 STARTTLS 升级失败。
// 判据与旧实现的 phaseTLS 一致：取消/超时 → 证书 →
// 5xx 视为 config（服务器拒绝升级属端口/模式配错，改配置之前重试
// 没有意义）→ 其余按连接类。
func wrapStartTLS(err error) error {
	if err == nil {
		return nil
	}
	kind, retryable := classifyStartTLS(err)
	return deliveryError(kind, retryable, err)
}

func classifyStartTLS(err error) (coremail.Kind, bool) {
	if kind, retryable, ok := timeoutOrCert(err); ok {
		return kind, retryable
	}
	if code, ok := smtpCode(err); ok && code/100 == 5 {
		return coremail.KindConfig, false
	}
	return coremail.KindConnect, true
}

// wrapAuth 分类认证失败。
// TLS 已在拨号或 STARTTLS 阶段建立，故除取消/超时/证书外一律不可重试：
// 凭据错重试只会再失败。
func wrapAuth(err error) error {
	if err == nil {
		return nil
	}
	if kind, retryable, ok := timeoutOrCert(err); ok {
		return deliveryError(kind, retryable, err)
	}
	return deliveryError(coremail.KindAuth, false, err)
}

// timeoutOrCert 是所有阶段共用的前置判据：取消/超时可重试，
// 证书类失败是本机配置问题不可重试。命中返回 true。
func timeoutOrCert(err error) (coremail.Kind, bool, bool) {
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return coremail.KindTimeout, true, true
	}
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return coremail.KindTimeout, true, true
	}
	var certErr *tls.CertificateVerificationError
	if errors.As(err, &certErr) {
		return coremail.KindConfig, false, true
	}
	var unknownAuthority x509.UnknownAuthorityError
	if errors.As(err, &unknownAuthority) {
		return coremail.KindConfig, false, true
	}
	var hostnameErr x509.HostnameError
	if errors.As(err, &hostnameErr) {
		return coremail.KindConfig, false, true
	}
	return "", false, false
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
