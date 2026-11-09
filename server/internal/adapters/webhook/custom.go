// 自定义 webhook 出口：POST {SECRET}（URL 本身即凭据），JSON {title, content}
//（保持旧线兼容格式）。成功判据是 2xx 且响应体里顶层带数值 code/errcode 的
// JSON 对象没有声明非 0（钉钉族「200 + 错误 body」判据，移植旧线）；
// 非 2xx：5xx/连接/超时按可重试分类，4xx 与 3xx 按 refused。
package webhook

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"

	corewebhook "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/webhook"
)

// sendCustom 发一条自定义 webhook 推送。
//
// 发送前跑两段 SSRF 复核：静态白名单（isSafeURL）+ 域名解析逐 IP 复核
// （fail-closed）。复核通过后用 DialContext 直连**已复核 IP**
//（不再二次解析，闭合 DNS rebinding 的 TOCTOU；TLS SNI 仍走原 host，
// 证书校验不变）。
func (s *Sender) sendCustom(ctx context.Context, push corewebhook.Push) error {
	ips, safe := s.resolveCustom(ctx)
	if !safe {
		return &corewebhook.DeliveryError{
			Kind: corewebhook.KindUnsafeURL,
			Err:  errors.New("webhook: custom URL failed the safety review"),
		}
	}
	ctx, cancel := sendCtx(ctx)
	defer cancel()
	body, err := json.Marshal(map[string]string{
		"title": push.Title, "content": push.Content,
	})
	if err != nil {
		return &corewebhook.DeliveryError{
			Kind: corewebhook.KindRefused,
			Err:  errors.New("webhook: cannot encode custom payload"),
		}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.secret, bytes.NewReader(body))
	if err != nil {
		// URL 无法解析是本地配置错误：白名单已拦掉大部分，这里是兜底。
		return &corewebhook.DeliveryError{
			Kind: corewebhook.KindUnsafeURL,
			Err:  errors.New("webhook: custom URL cannot be parsed"),
		}
	}
	req.Header.Set("Content-Type", "application/json")
	client := noRedirect(s.customClient(ips))
	resp, err := client.Do(req)
	if err != nil {
		return classifyTransport(err)
	}
	defer drainClose(resp.Body)
	// 非 2xx：跳转目标从未经过白名单，3xx 不是投递；4xx 是确定性拒收；
	// 5xx 是对端暂时故障，按可重试的连接类失败。状态码不进日志与错误文本：
	// 持被窃会话者可把目标指向内网主机，再从日志读回端口探测结果。
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		kind := corewebhook.KindRefused
		if resp.StatusCode >= 500 {
			kind = corewebhook.KindConnect
		}
		return &corewebhook.DeliveryError{
			Kind: kind, Retryable: kind == corewebhook.KindConnect,
			Err: errors.New("webhook: custom endpoint answered non-2xx"),
		}
	}
	// 回执判定：只对顶层带数值 code/errcode 的 JSON 对象参与——
	// 非 JSON、非对象、值非数值的响应维持「HTTP 状态为准」，不误杀
	// 未声明协议的端点。回执拒收按 refused，不可重试。
	if receipt, err := readReceipt(resp); err == nil && receiptRefuses(receipt) {
		return &corewebhook.DeliveryError{
			Kind: corewebhook.KindRefused,
			Err:  errors.New("webhook: custom endpoint reported a non-zero business code"),
		}
	}
	return nil
}

// resolveCustom 对 custom 地址跑静态 + 解析复核，返回复核通过的 IP 列表。
// 判定与返回**用同一次解析结果**：如果判定用一次解析、直连再用另一次，
// 两次之间 DNS 可以变脸，复核就被绕过了。
// 任一段不过即不安全（fail-closed），返回 nil。
// IP 字面量目标返回空列表（静态层已判，DialContext 直接按原地址拨）。
func (s *Sender) resolveCustom(ctx context.Context) ([]net.IPAddr, bool) {
	if !isSafeURL(s.secret) {
		return nil, false
	}
	parsed, err := url.Parse(s.secret)
	if err != nil {
		return nil, false
	}
	host := normalizeHost(parsed.Hostname())
	if host == "" {
		return nil, false
	}
	if net.ParseIP(host) != nil {
		return nil, true
	}
	lookup := s.lookup
	if lookup == nil {
		lookup = defaultLookup
	}
	ips, err := lookup(ctx, host)
	if err != nil || len(ips) == 0 {
		return nil, false
	}
	for _, addr := range ips {
		// 剥 v6 zone 不必要：zone 只在链路本地地址出现，isNonroutableTarget
		// 按裸 IP 判（IPAddr.Zone 不参与）。
		if isNonroutableTarget(addr.IP) {
			return nil, false
		}
	}
	return ips, true
}

// customClient 构造 custom 出口的 HTTP 客户端：DialContext 直连已复核 IP，
// 不做第二次 DNS 解析；TLS SNI 与证书校验仍按原 host（transport 的
// ServerName 取自请求 URL，与 DialContext 收到的拨号地址无关）。
// ips 为空（IP 字面量目标）时按原始地址拨号。
// 测试注入了带 Transport 的 Client 时优先用注入面：假 transport 不落真网络。
func (s *Sender) customClient(ips []net.IPAddr) *http.Client {
	if s.client != nil && s.client.Transport != nil {
		return s.client
	}
	dialer := &net.Dialer{Timeout: DefaultTimeout}
	transport := &http.Transport{
		Proxy:               nil, // 推送目标必须是我们复核过的直连地址，不走环境代理
		DisableKeepAlives:   true,
		TLSHandshakeTimeout: DefaultTimeout,
		DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			candidates, err := dialAddrs(addr, ips)
			if err != nil {
				return nil, err
			}
			// 逐个已复核 IP 尝试：第一个连通的返回，全部失败带首个错误。
			var firstErr error
			for _, dialTo := range candidates {
				conn, dialErr := dialer.DialContext(ctx, network, dialTo)
				if dialErr == nil {
					return conn, nil
				}
				if firstErr == nil {
					firstErr = dialErr
				}
			}
			return nil, firstErr
		},
	}
	return &http.Client{
		Timeout:   DefaultTimeout,
		Transport: transport,
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
}

// dialAddrs 把 transport 给的拨号地址改写成「已复核 IP + 原端口」候选列表：
// host 换成复核 IP（不再二次解析），端口保留。IP 字面量目标（ips 为空）
// 原样返回单候选。这一步是 DNS rebinding TOCTOU 的闭合点。
func dialAddrs(addr string, ips []net.IPAddr) ([]string, error) {
	if len(ips) == 0 {
		return []string{addr}, nil
	}
	_, port, err := net.SplitHostPort(addr)
	if err != nil {
		return nil, err
	}
	out := make([]string, 0, len(ips))
	for _, ip := range ips {
		out = append(out, net.JoinHostPort(ip.IP.String(), port))
	}
	return out, nil
}

// readReceipt 读响应体并解析为 JSON 对象。非 JSON 返回错误，
// 回执判定按「未声明协议」维持 HTTP 口径（不算拒收也不算坏响应）。
func readReceipt(resp *http.Response) (map[string]any, error) {
	raw, err := io.ReadAll(io.LimitReader(resp.Body, maxResponseBody))
	if err != nil {
		return nil, err
	}
	var payload map[string]any
	if err := json.Unmarshal(raw, &payload); err != nil {
		return nil, err
	}
	return payload, nil
}

// receiptRefuses 是「200 + 错误 body」判据：顶层带数值 code/errcode 的
// JSON 对象声明非 0 即拒收。非 JSON、非对象、值非数值都不定罪。
func receiptRefuses(payload map[string]any) bool {
	for _, key := range []string{"code", "errcode"} {
		value, ok := payload[key]
		if !ok {
			continue
		}
		if numeric, ok := value.(float64); ok && numeric != 0 {
			return true
		}
	}
	return false
}

// drainClose 读完剩余响应体再关闭：让连接可被复用（或直接丢弃）。
func drainClose(body io.ReadCloser) {
	_, _ = io.Copy(io.Discard, io.LimitReader(body, maxResponseBody))
	_ = body.Close()
}
