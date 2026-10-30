package ratelimit

// 客户端 IP 与可信代理表。
//
// # 功能
// 解析逗号分隔的 CIDR 表；按「直连对端是否可信」决定是否采信
// X-Forwarded-For。本文件**不**读环境变量、**不**信任任意来源的转发头。
//
// # 口径
// 无可信代理时只看 RemoteAddr：伪造成本的 XFF 一律忽略。
// 对端可信时从右往左取第一个不在可信表内的地址：右侧是本机看到的最外层代理。
// 全部可信或头部畸形时回退到直连对端，绝不返回空串。

import (
	"fmt"
	"net"
	"net/netip"
	"strings"
)

// Prefix 是可信代理网段。
type Prefix = netip.Prefix

// ParseTrustedProxies 解析逗号分隔的 CIDR 或裸 IP。
// 裸 IP 按单机网段处理（IPv4 /32、IPv6 /128）。
// 空串返回空表：缺省不信任任何代理。
func ParseTrustedProxies(raw string) ([]Prefix, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, nil
	}
	var out []Prefix
	for _, part := range strings.Split(raw, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		if strings.Contains(part, "/") {
			prefix, err := netip.ParsePrefix(part)
			if err != nil {
				return nil, fmt.Errorf("%w %q: %v", ErrNoTrustedProxy, part, err)
			}
			out = append(out, prefix.Masked())
			continue
		}
		addr, err := netip.ParseAddr(part)
		if err != nil {
			return nil, fmt.Errorf("%w %q: %v", ErrNoTrustedProxy, part, err)
		}
		out = append(out, netip.PrefixFrom(addr, addr.BitLen()))
	}
	return out, nil
}

// ClientIP 返回请求的客户端地址。
// remoteAddr 是 net/http 的 RemoteAddr（host:port 或裸 host）。
// forwarded 是 X-Forwarded-For 的逐行值（未拆分逗号也可）。
// trusted 为空表示不信任任何转发头。
func ClientIP(remoteAddr string, forwarded []string, trusted []Prefix) string {
	host := remoteAddr
	if h, _, err := net.SplitHostPort(remoteAddr); err == nil {
		host = h
	}
	peer, err := netip.ParseAddr(host)
	if err != nil {
		return host
	}
	if !isTrusted(peer, trusted) {
		return peer.String()
	}
	// 先按「头部顺序、再按逗号」展平成一条链，然后从右往左走。
	// 右侧是离本机最近的一跳；最左值可由客户端伪造。
	var hops []string
	for _, value := range forwarded {
		for _, part := range strings.Split(value, ",") {
			if part = strings.TrimSpace(part); part != "" {
				hops = append(hops, part)
			}
		}
	}
	for i := len(hops) - 1; i >= 0; i-- {
		addr, err := netip.ParseAddr(hops[i])
		if err != nil {
			continue
		}
		if isTrusted(addr, trusted) {
			continue
		}
		return addr.String()
	}
	return peer.String()
}

// isTrusted 报告地址是否落在可信表内。
func isTrusted(addr netip.Addr, trusted []Prefix) bool {
	for _, prefix := range trusted {
		if prefix.Contains(addr) {
			return true
		}
	}
	return false
}

// IsTrustedPeer 报告直连对端是否在可信表内。
// 用途：只有可信代理的 X-Forwarded-* 头才可采信。
func IsTrustedPeer(remoteAddr string, trusted []Prefix) bool {
	host := remoteAddr
	if h, _, err := net.SplitHostPort(remoteAddr); err == nil {
		host = h
	}
	addr, err := netip.ParseAddr(host)
	if err != nil {
		return false
	}
	return isTrusted(addr, trusted)
}
