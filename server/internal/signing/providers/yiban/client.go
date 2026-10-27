package yiban

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"io"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"strings"
	"sync"
	"time"

	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/protocol"
)

const (
	defaultTimeout    = 15 * time.Second
	defaultSessionTTL = 30 * time.Minute
	maxBodyBytes      = 1 << 20
	maxMessageRunes   = 200
	maxAuthRedirects  = 5
)

// Config 是客户端的运行参数。
// 零值可用：端点、时钟、超时、TTL 与随机源都有默认值。
type Config struct {
	Endpoints  Endpoints
	Clock      coreclock.Clock
	Timeout    time.Duration
	SessionTTL time.Duration
	Transport  http.RoundTripper
	Random     io.Reader
}

// Client 维护每账号的内存会话。
// 单进程内使用；不同账号可并行，同账号的调用按会话互斥。
type Client struct {
	endpoints Endpoints
	clock     coreclock.Clock
	timeout   time.Duration
	ttl       time.Duration
	transport http.RoundTripper
	random    io.Reader

	mu       sync.Mutex
	sessions map[string]*sessionEntry

	posMu     sync.Mutex
	positions map[string]positionEntry
}

type positionEntry struct {
	at  time.Time
	cfg protocol.SignPositionConfig
}

type sessionEntry struct {
	mu      sync.Mutex
	http    *http.Client
	csrf    string
	expires time.Time
}

// New 构造客户端。零值配置取默认值。
func New(cfg Config) *Client {
	if cfg.Endpoints == (Endpoints{}) {
		cfg.Endpoints = DefaultEndpoints()
	}
	if cfg.Clock == nil {
		cfg.Clock = coreclock.System{}
	}
	if cfg.Timeout <= 0 {
		cfg.Timeout = defaultTimeout
	}
	if cfg.SessionTTL <= 0 {
		cfg.SessionTTL = defaultSessionTTL
	}
	if cfg.Random == nil {
		cfg.Random = rand.Reader
	}
	return &Client{
		endpoints: cfg.Endpoints,
		clock:     cfg.Clock,
		timeout:   cfg.Timeout,
		ttl:       cfg.SessionTTL,
		transport: cfg.Transport,
		random:    cfg.Random,
		sessions:  make(map[string]*sessionEntry),
		positions: make(map[string]positionEntry),
	}
}

func (c *Client) entry(accountID string) *sessionEntry {
	c.mu.Lock()
	defer c.mu.Unlock()
	entry := c.sessions[accountID]
	if entry == nil {
		entry = &sessionEntry{}
		c.sessions[accountID] = entry
	}
	return entry
}

func (e *sessionEntry) valid(now time.Time) bool {
	return e.http != nil && now.Before(e.expires)
}

// Verify 执行一次完整登录并刷新会话缓存。
// 它总是重新登录：调用方用它校验凭据，缓存是否命中不改变结论。
func (c *Client) Verify(ctx context.Context, accountID string, cred contract.Credential) (contract.Profile, error) {
	entry := c.entry(accountID)
	entry.mu.Lock()
	defer entry.mu.Unlock()
	return c.loginLocked(ctx, entry, cred)
}

// withSession 复用会话执行一次平台调用。
// 会话缺失或过期时先登录；遇到 999 失效重登一次。
// 调用期间持有该账号的锁，避免并发登录与串话。
// **回调必须可安全重试**：999 后会再调用一次；签到等非幂等 POST
// 由调用方按 AttemptID 去重（P3-D）。
func (c *Client) withSession(ctx context.Context, accountID string, cred contract.Credential,
	fn func(context.Context, *http.Client, string) error) error {
	entry := c.entry(accountID)
	entry.mu.Lock()
	defer entry.mu.Unlock()
	if !entry.valid(c.clock.Now()) {
		if _, err := c.loginLocked(ctx, entry, cred); err != nil {
			return err
		}
	}
	err := fn(ctx, entry.http, entry.csrf)
	var expired *protocol.SessionExpired
	if errors.As(err, &expired) {
		c.clearPositions(accountID)
		entry.http = nil
		if _, loginErr := c.loginLocked(ctx, entry, cred); loginErr != nil {
			return loginErr
		}
		return fn(ctx, entry.http, entry.csrf)
	}
	if err == nil {
		entry.expires = c.clock.Now().Add(c.ttl)
	}
	return err
}

// withSessionNoRetry 与 withSession 相同，但 999 不自动重试。
// 供非幂等的签到 POST 使用：重登后重发可能重复提交。
func (c *Client) withSessionNoRetry(ctx context.Context, accountID string, cred contract.Credential,
	fn func(context.Context, *http.Client, string) error) error {
	entry := c.entry(accountID)
	entry.mu.Lock()
	defer entry.mu.Unlock()
	if !entry.valid(c.clock.Now()) {
		if _, err := c.loginLocked(ctx, entry, cred); err != nil {
			return err
		}
	}
	err := fn(ctx, entry.http, entry.csrf)
	if err == nil {
		entry.expires = c.clock.Now().Add(c.ttl)
	}
	return err
}

func (c *Client) clearSession(accountID string) {
	entry := c.entry(accountID)
	entry.mu.Lock()
	entry.http = nil
	entry.mu.Unlock()
	c.clearPositions(accountID)
}

func (c *Client) clearPositions(accountID string) {
	c.posMu.Lock()
	delete(c.positions, accountID)
	c.posMu.Unlock()
}

func (c *Client) newHTTPClient() *http.Client {
	jar, _ := cookiejar.New(nil)
	return &http.Client{
		Transport:     c.transport,
		Jar:           jar,
		CheckRedirect: noRedirect,
		Timeout:       c.timeout,
	}
}

// addCSRF 每跳显式带上 csrf_token。
// 平台的该 cookie 域名是空的，参考实现对任意主机一并带出；
// Go 的 cookiejar 会做主机绑定，故不能用 jar 代替本调用。
func addCSRF(req *http.Request, csrf string) {
	req.AddCookie(&http.Cookie{Name: "csrf_token", Value: csrf})
}

// do 执行请求并读取有上限的响应体。**不跟随重定向**。
func (c *Client) do(client *http.Client, req *http.Request) (*http.Response, []byte, error) {
	resp, err := client.Do(req)
	if err != nil {
		return nil, nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxBodyBytes+1))
	if err != nil {
		return nil, nil, err
	}
	if len(body) > maxBodyBytes {
		return nil, nil, errors.New("yiban: response body is too large")
	}
	return resp, body, nil
}

func setLoginHeaders(req *http.Request) {
	req.Header.Set("User-Agent", "Yiban")
	req.Header.Set("AppVersion", appVersion)
	req.Header.Set("Origin", loginOrigin)
	req.Header.Set("Referer", loginOrigin+"/")
	req.Header.Set("Connection", "close")
}

// setUsersureHeaders 提交账号密码时的头。
// **不得带 Origin/Referer**：带了会得 e001 无效应用端编号。
func setUsersureHeaders(req *http.Request) {
	req.Header.Set("User-Agent", "Yiban")
	req.Header.Set("AppVersion", appVersion)
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
	req.Header.Set("Connection", "close")
}

// setAppHeaders 是签到链的头：Origin/Referer 必须改回 App 域。
func setAppHeaders(req *http.Request) {
	req.Header.Set("User-Agent", "Yiban")
	req.Header.Set("AppVersion", appVersion)
	req.Header.Set("Origin", appOrigin)
	req.Header.Set("Referer", appOrigin+"/")
	req.Header.Set("Connection", "close")
}

func newRequest(ctx context.Context, method, rawURL string, body io.Reader) (*http.Request, error) {
	return http.NewRequestWithContext(ctx, method, rawURL, body)
}

func withQuery(base string, values url.Values) string {
	parsed, err := url.Parse(base)
	if err != nil {
		return base
	}
	query := parsed.Query()
	for key, list := range values {
		for _, value := range list {
			query.Set(key, value)
		}
	}
	parsed.RawQuery = query.Encode()
	return parsed.String()
}

// withQueryOrdered 按给定顺序拼接 query。
// url.Values.Encode 会按字母序输出，与旧实现的请求行顺序不符时用本函数。
func withQueryOrdered(base string, pairs [][2]string) string {
	separator := "?"
	if strings.Contains(base, "?") {
		separator = "&"
	}
	parts := make([]string, 0, len(pairs))
	for _, pair := range pairs {
		parts = append(parts, url.QueryEscape(pair[0])+"="+url.QueryEscape(pair[1]))
	}
	return base + separator + strings.Join(parts, "&")
}

func randomHex(reader io.Reader, n int) (string, error) {
	buf := make([]byte, n)
	if _, err := io.ReadFull(reader, buf); err != nil {
		return "", err
	}
	return hex.EncodeToString(buf), nil
}

func noRedirect(*http.Request, []*http.Request) error {
	return http.ErrUseLastResponse
}

// trustedAuthHost 只允许认证跳回落到配置的 API 端点或 api.uyiban.com。
// 生产只接受 https；测试端点按配置的 scheme 比较。
// 白名单比参考实现窄：只认 API 主机，其余主机一律拒绝（fail-closed）。
func (c *Client) trustedAuthHost(target *url.URL) bool {
	if target == nil || target.Host == "" || target.Scheme == "" {
		return false
	}
	if api, err := url.Parse(c.endpoints.APIAuth); err == nil && target.Host == api.Host {
		return target.Scheme == api.Scheme
	}
	return target.Scheme == "https" && target.Host == "api.uyiban.com"
}

// safeMessage 把平台文案压成可进审计的短消息。
func safeMessage(s string) string {
	s = mask.MaskText(s)
	runes := []rune(s)
	if len(runes) > maxMessageRunes {
		s = string(runes[:maxMessageRunes])
	}
	return s
}

func outcome(o contract.Outcome, msg string) error {
	return &contract.OutcomeError{Outcome: o, Message: safeMessage(msg)}
}
