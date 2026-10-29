// Package mailtest 提供进程内的最小 SMTP 服务器，供适配器测试与端到端测试共用。
//
// # 功能
// 监听 127.0.0.1 的随机端口，实现 EHLO、STARTTLS、隐式 TLS、AUTH PLAIN、
// MAIL、RCPT、DATA、QUIT。记录每封邮件的信封与正文。
// 本包**不**投递真实邮件，**不**校验证书链以外的安全属性。
//
// # 归属
// adapters/mail 的测试支撑件。生产代码不导入本包；
// 放在普通包里是为了让 cmd/server 的 e2e 复用同一份假服务器，
// 避免两套手写 SMTP 对话各自漂移。
//
// # 复用
// Start、Mode、ImplicitTLS、StartTLS；Server.Addr、Port、TLSConfig、
// SetCredentials、SetAuthFail、SetRejectRecipient、SetGreetingDelay、
// Messages、MessageCount、WaitMessages、Close。
//
// # 口径
// 证书自签，有效期固定 2020-01-01 至 2100-01-01：测试不读系统时钟造证书。
// 服务器收到 DATA 的正文即视为已接收；测试用 Messages 读取。
// 所有等待都有上界；WaitMessages 超时返回 false，不永久挂起。
package mailtest

import (
	"bufio"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"errors"
	"fmt"
	"math/big"
	"net"
	"strings"
	"sync"
	"time"
)

// Mode 是 TLS 建立方式。
type Mode int

const (
	// ImplicitTLS 在 TCP 连接建立后立即握手（对应 465 端口）。
	ImplicitTLS Mode = iota
	// StartTLS 先明文 EHLO，再按客户端命令升级（对应 587 等端口）。
	StartTLS
)

// Message 是一封被服务器接收的邮件。
type Message struct {
	From     string
	To       []string
	Data     string
	AuthUser string
}

// Server 是最小 SMTP 服务器。
type Server struct {
	mode   Mode
	cert   tls.Certificate
	roots  *x509.CertPool
	ln     net.Listener
	addr   string
	closed chan struct{}

	mu              sync.Mutex
	messages        []Message
	username        string
	password        string
	authFail        bool
	rejectRecipient bool
	greetingDelay   time.Duration

	connMu sync.Mutex
	conns  map[net.Conn]struct{}
	wg     sync.WaitGroup
}

// Start 启动服务器。调用方必须用 Close 收尾。
func Start(mode Mode) (*Server, error) {
	cert, roots, err := selfSigned()
	if err != nil {
		return nil, err
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return nil, err
	}
	if mode == ImplicitTLS {
		ln = tls.NewListener(ln, &tls.Config{
			Certificates: []tls.Certificate{cert},
			MinVersion:   tls.VersionTLS12,
		})
	}
	s := &Server{
		mode:   mode,
		cert:   cert,
		roots:  roots,
		ln:     ln,
		addr:   ln.Addr().String(),
		closed: make(chan struct{}),
		conns:  map[net.Conn]struct{}{},
	}
	s.wg.Add(1)
	go s.serve()
	return s, nil
}

// Addr 返回 host:port。
func (s *Server) Addr() string { return s.addr }

// Port 返回监听端口。
func (s *Server) Port() int {
	_, port, err := net.SplitHostPort(s.addr)
	if err != nil {
		return 0
	}
	n := 0
	for _, r := range port {
		if r < '0' || r > '9' {
			return 0
		}
		n = n*10 + int(r-'0')
	}
	return n
}

// TLSConfig 返回客户端信任根证书的配置。ServerName 为空，由适配器按 host 填。
func (s *Server) TLSConfig() *tls.Config {
	return &tls.Config{RootCAs: s.roots, MinVersion: tls.VersionTLS12}
}

// SetCredentials 设置 AUTH PLAIN 接受的用户名与口令。两者都为空表示接受任意值。
func (s *Server) SetCredentials(user, pass string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.username, s.password = user, pass
}

// SetAuthFail 让 AUTH 一律回 535。
func (s *Server) SetAuthFail(fail bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.authFail = fail
}

// SetRejectRecipient 让 RCPT 回 550。
func (s *Server) SetRejectRecipient(reject bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.rejectRecipient = reject
}

// SetGreetingDelay 延迟 220 欢迎行，用于触发客户端超时。
func (s *Server) SetGreetingDelay(d time.Duration) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.greetingDelay = d
}

// Messages 返回已接收邮件的副本。
func (s *Server) Messages() []Message {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]Message, len(s.messages))
	copy(out, s.messages)
	return out
}

// MessageCount 返回已接收邮件数。
func (s *Server) MessageCount() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.messages)
}

// WaitMessages 等到至少 n 封邮件或超时。返回值报告是否达到 n。
func (s *Server) WaitMessages(n int, timeout time.Duration) bool {
	deadline := time.After(timeout)
	tick := time.NewTicker(10 * time.Millisecond)
	defer tick.Stop()
	for {
		if s.MessageCount() >= n {
			return true
		}
		select {
		case <-deadline:
			return s.MessageCount() >= n
		case <-s.closed:
			return s.MessageCount() >= n
		case <-tick.C:
		}
	}
}

// Close 停止监听并关闭全部在途连接。
func (s *Server) Close() {
	select {
	case <-s.closed:
		return
	default:
	}
	close(s.closed)
	_ = s.ln.Close()
	s.connMu.Lock()
	for c := range s.conns {
		_ = c.Close()
	}
	s.connMu.Unlock()
	s.wg.Wait()
}

func (s *Server) track(c net.Conn) {
	s.connMu.Lock()
	defer s.connMu.Unlock()
	s.conns[c] = struct{}{}
}

func (s *Server) untrack(c net.Conn) {
	s.connMu.Lock()
	defer s.connMu.Unlock()
	delete(s.conns, c)
}

func (s *Server) serve() {
	defer s.wg.Done()
	for {
		conn, err := s.ln.Accept()
		if err != nil {
			return
		}
		s.track(conn)
		s.wg.Add(1)
		go func() {
			defer s.wg.Done()
			defer s.untrack(conn)
			s.handle(conn)
		}()
	}
}

func (s *Server) handle(conn net.Conn) {
	defer conn.Close()
	br := bufio.NewReader(conn)

	s.mu.Lock()
	delay := s.greetingDelay
	s.mu.Unlock()
	if delay > 0 {
		select {
		case <-time.After(delay):
		case <-s.closed:
			return
		}
	}
	if !writeLine(conn, "220 mailtest ESMTP") {
		return
	}

	secure := s.mode == ImplicitTLS
	var (
		from     string
		rcpts    []string
		authUser string
	)
	for {
		line, err := br.ReadString('\n')
		if err != nil {
			return
		}
		line = strings.TrimRight(line, "\r\n")
		upper := strings.ToUpper(line)

		switch {
		case strings.HasPrefix(upper, "EHLO"):
			if !secure && s.mode == StartTLS {
				if !writeLine(conn, "250-mailtest\r\n250-STARTTLS\r\n250 AUTH PLAIN") {
					return
				}
			} else if !writeLine(conn, "250-mailtest\r\n250 AUTH PLAIN") {
				return
			}
		case strings.HasPrefix(upper, "HELO"):
			if !writeLine(conn, "250 mailtest") {
				return
			}
		case upper == "STARTTLS" && s.mode == StartTLS && !secure:
			if !writeLine(conn, "220 2.0.0 ready to start TLS") {
				return
			}
			tlsConn := tls.Server(conn, &tls.Config{
				Certificates: []tls.Certificate{s.cert},
				MinVersion:   tls.VersionTLS12,
			})
			if err := tlsConn.Handshake(); err != nil {
				return
			}
			conn = tlsConn
			br = bufio.NewReader(conn)
			secure = true
		case strings.HasPrefix(upper, "AUTH PLAIN"):
			user, ok := s.checkAuth(line)
			if !ok {
				if !writeLine(conn, "535 5.7.8 authentication failed") {
					return
				}
				continue
			}
			authUser = user
			if !writeLine(conn, "235 2.7.0 authentication successful") {
				return
			}
		case strings.HasPrefix(upper, "MAIL FROM:"):
			from = strings.Trim(strings.TrimSpace(line[len("MAIL FROM:"):]), "<>")
			if !writeLine(conn, "250 2.1.0 ok") {
				return
			}
		case strings.HasPrefix(upper, "RCPT TO:"):
			s.mu.Lock()
			reject := s.rejectRecipient
			s.mu.Unlock()
			if reject {
				if !writeLine(conn, "550 5.1.1 no such user") {
					return
				}
				continue
			}
			rcpts = append(rcpts, strings.Trim(strings.TrimSpace(line[len("RCPT TO:"):]), "<>"))
			if !writeLine(conn, "250 2.1.5 ok") {
				return
			}
		case upper == "DATA":
			if !writeLine(conn, "354 end with <CRLF>.<CRLF>") {
				return
			}
			data, ok := readData(br)
			if !ok {
				return
			}
			s.mu.Lock()
			s.messages = append(s.messages, Message{
				From: from, To: append([]string(nil), rcpts...), Data: data, AuthUser: authUser,
			})
			s.mu.Unlock()
			if !writeLine(conn, "250 2.0.0 queued") {
				return
			}
		case upper == "RSET":
			from, rcpts = "", nil
			if !writeLine(conn, "250 2.0.0 ok") {
				return
			}
		case upper == "NOOP":
			if !writeLine(conn, "250 2.0.0 ok") {
				return
			}
		case upper == "QUIT":
			_ = writeLine(conn, "221 2.0.0 bye")
			return
		default:
			if !writeLine(conn, "502 5.5.2 command not implemented") {
				return
			}
		}
	}
}

// checkAuth 解析 AUTH PLAIN 的 base64 参数并对账配置。
func (s *Server) checkAuth(line string) (string, bool) {
	fields := strings.Fields(line)
	if len(fields) < 3 {
		return "", false
	}
	raw, err := base64.StdEncoding.DecodeString(fields[2])
	if err != nil {
		return "", false
	}
	parts := strings.Split(string(raw), "\x00")
	if len(parts) != 3 {
		return "", false
	}
	user, pass := parts[1], parts[2]
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.authFail {
		return "", false
	}
	if s.username == "" && s.password == "" {
		// 未配置期望值时接受任意凭据：测试只关心客户端发了什么。
		return user, true
	}
	if user != s.username || pass != s.password {
		return "", false
	}
	return user, true
}

// readData 读到单独的 "." 行。返回去掉行尾的正文。
func readData(br *bufio.Reader) (string, bool) {
	var b strings.Builder
	for {
		line, err := br.ReadString('\n')
		if err != nil {
			return "", false
		}
		if line == ".\r\n" || line == ".\n" {
			return b.String(), true
		}
		b.WriteString(line)
	}
}

func writeLine(conn net.Conn, line string) bool {
	if !strings.HasSuffix(line, "\r\n") {
		line += "\r\n"
	}
	_, err := conn.Write([]byte(line))
	return err == nil
}

// selfSigned 生成测试证书与客户端信任池。
// 有效期固定：测试不读系统时钟造证书（canonical 的 clock.now 只许 core/clock 调用）。
func selfSigned() (tls.Certificate, *x509.CertPool, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return tls.Certificate{}, nil, err
	}
	notBefore := time.Date(2020, 1, 1, 0, 0, 0, 0, time.UTC)
	notAfter := time.Date(2100, 1, 1, 0, 0, 0, 0, time.UTC)
	tmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "127.0.0.1"},
		NotBefore:             notBefore,
		NotAfter:              notAfter,
		KeyUsage:              x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		BasicConstraintsValid: true,
		IsCA:                  true,
		IPAddresses:           []net.IP{net.ParseIP("127.0.0.1")},
		DNSNames:              []string{"localhost"},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		return tls.Certificate{}, nil, err
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		return tls.Certificate{}, nil, err
	}
	roots := x509.NewCertPool()
	roots.AddCert(cert)
	return tls.Certificate{
		Certificate: [][]byte{der},
		PrivateKey:  key,
		Leaf:        cert,
	}, roots, nil
}

// HostPort 拆分 host 与端口。测试与适配器共用同一口径。
func HostPort(addr string) (string, int, error) {
	host, port, err := net.SplitHostPort(addr)
	if err != nil {
		return "", 0, err
	}
	n := 0
	if port == "" {
		return "", 0, errors.New("mailtest: empty port")
	}
	for _, r := range port {
		if r < '0' || r > '9' {
			return "", 0, fmt.Errorf("mailtest: non-numeric port %q", port)
		}
		n = n*10 + int(r-'0')
	}
	return host, n, nil
}
