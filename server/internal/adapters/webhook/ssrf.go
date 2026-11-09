// 自定义 webhook 地址的 SSRF 白名单：判据逐字移植旧线
// yiban/adapters/notifier/notify/config.py 的 is_safe_url /
// is_safe_url_resolved / _is_nonroutable_target / _is_ipv4_literal_like。
//
// 口径：
// 仅 https；含反斜杠一律拒（Go 的 net/url 与旧线 urllib3 对反斜杠的
// userinfo 切分不一致，fail-closed：正常 URL 不含反斜杠，零误杀）；
// host `localhost` 与尾点归一后同名拒；`ipaddress` 家族判定
// （loopback/private/link_local/unspecified/multicast/reserved/
// site_local/CGNAT 100.64.0.0/10/IPv4-mapped IPv6）；IPv4 字面量变体
// 全拒（全十进制整数、0x 十六进制、前导零八进制、`127.1` 短式）。
// 域名先解析、逐 IP 复核，解析失败拒（fail-closed）；字面量已在
// 静态层判定，无需解析。带 userinfo 的 host 不在收严之列：
// `https://user:pass@host` 是合法的 webhook basic-auth 写法，且
// `https://evil.com@127.0.0.1` 的 host 就是 127.0.0.1，被内网判定拦下。
package webhook

import (
	"context"
	"net"
	"net/url"
	"strings"
)

// cgnatV4 是 RFC 6598 CGNAT 段：`net.IP.IsPrivate` 不覆盖，而云厂商
// 元数据服务常落在此段（阿里云 100.100.100.200 即在其中）。
const cgnatV4First, cgnatV4Last = 64, 127 // 100.64.0.0/10 的第二字节域

// lookupIP 是域名解析的注入点：测试传假解析器，生产用 net.DefaultResolver。
type lookupIP func(ctx context.Context, host string) ([]net.IPAddr, error)

// isSafeURL 是自定义地址的静态白名单：https + 非回环/内网/链路本地/
// 未指定/CGNAT/组播/保留段 + 拒 IPv4 字面量变体与反斜杠。
// 域名目标放行到静态层，但发送前必须再过 isSafeURLResolved。
func isSafeURL(raw string) bool {
	if strings.Contains(raw, "\\") {
		return false
	}
	parsed, err := url.Parse(raw)
	if err != nil {
		return false
	}
	if parsed.Scheme != "https" || parsed.Hostname() == "" {
		return false
	}
	host := normalizeHost(parsed.Hostname())
	if host == "localhost" {
		return false
	}
	if ip := net.ParseIP(host); ip != nil {
		return !isNonroutableTarget(ip)
	}
	// 非 net.ParseIP 可解析的 host：若形如 IPv4 字面量变体则拒；
	// 只有真域名（含点号且非全数字组件）放行到解析复核。
	return !isIPv4LiteralLike(host)
}

// isSafeURLResolved 是发送期复核：静态白名单 + 域名解析后逐 IP 复核。
// 静态白名单对域名一律放行，而「DNS 解析到 169.254.169.254 的域名」
// 只能靠解析复核拦下；解析失败同样拒（fail-closed）。
// 解析与连接之间的 DNS rebinding TOCTOU 由调用方用 DialContext
// 直连已复核 IP 闭合（见 custom.go 的 verifiedDialer）。
func isSafeURLResolved(ctx context.Context, raw string, lookup lookupIP) bool {
	if !isSafeURL(raw) {
		return false
	}
	parsed, err := url.Parse(raw)
	if err != nil {
		return false
	}
	host := normalizeHost(parsed.Hostname())
	if host == "" {
		return false
	}
	if net.ParseIP(host) != nil {
		return true // 字面量：静态层已判过，无需解析
	}
	if lookup == nil {
		lookup = defaultLookup
	}
	ips, err := lookup(ctx, host)
	if err != nil || len(ips) == 0 {
		return false
	}
	for _, addr := range ips {
		// 剥离 v6 zone（%eth0）：zone 只出现在链路本地地址，剥出裸 IP 再判。
		if isNonroutableTarget(addr.IP) {
			return false
		}
	}
	return true
}

// defaultLookup 是生产解析器。
func defaultLookup(ctx context.Context, host string) ([]net.IPAddr, error) {
	return net.DefaultResolver.LookupIPAddr(ctx, host)
}

// normalizeHost 归一 host：去首尾空白、小写、去一个尾点。
// 与旧线 `o.hostname.strip().lower().rstrip(".")` 同口径
// （`localhost.` 与 `localhost` 同判）。
func normalizeHost(hostname string) string {
	return strings.ToLower(strings.TrimSuffix(strings.TrimSpace(hostname), "."))
}

// isNonroutableTarget 报告该 IP 是否属于「不得作为推送目标」的地址段。
// 这是 SSRF 白名单的唯一地址族判据。
func isNonroutableTarget(ip net.IP) bool {
	if ip4 := ip.To4(); ip4 != nil {
		// `::ffff:a.b.c.d` 形态的 IPv4-mapped：按其映射的 v4 判
		//（与旧线 ipv4_mapped 显式下钻同口径）。
		if ip4.IsLoopback() || ip4.IsPrivate() || ip4.IsLinkLocalUnicast() ||
			ip4.IsUnspecified() || ip4.IsMulticast() {
			return true
		}
		return isReservedV4(ip4) || isCGNATV4(ip4)
	}
	return ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast() ||
		ip.IsUnspecified() || ip.IsMulticast() || isSiteLocalV6(ip) ||
		isDocumentationV6(ip)
}

// isReservedV4 覆盖 240.0.0.0/4（首字节 240..255，含 255.255.255.255）：
// 与旧线 ipaddress.is_reserved 的 v4 覆盖域一致。
func isReservedV4(ip4 net.IP) bool {
	return ip4[0] >= 240
}

// isCGNATV4 判 100.64.0.0/10。
func isCGNATV4(ip4 net.IP) bool {
	return ip4[0] == 100 && ip4[1] >= cgnatV4First && ip4[1] <= cgnatV4Last
}

// isSiteLocalV6 判 fec0::/10（已废弃但仍不得作为推送目标的站点本地段）。
func isSiteLocalV6(ip net.IP) bool {
	return ip[0] == 0xfe && ip[1]&0xc0 == 0x80
}

// isDocumentationV6 判 2001:db8::/32（文档示例段，现实内网形态之外
// 最常见的不可路由 v6 段）。
func isDocumentationV6(ip net.IP) bool {
	return ip[0] == 0x20 && ip[1] == 0x01 && ip[2] == 0x0d && ip[3] == 0xb8
}

// isIPv4LiteralLike 报告 host 是否形如 IPv4 字面量（net.ParseIP 解析不了
// 的非标准写法）。覆盖：全数字（十进制整数，含 `2130706433` = 127.0.0.1）、
// `0x` 十六进制（`0x7f000001`）、前导零八进制（`0177.0.0.1`）、
// 短式回环（`127.1`，点分但末段缺失）。这些都会在连接时被解析为
// 内网/回环地址，故一律拒。含字母（真域名）返回 false。
// host 已经过 normalizeHost（无前缀/尾点）。
func isIPv4LiteralLike(host string) bool {
	if host == "" {
		return false
	}
	parts := strings.Split(host, ".")
	segOK := func(seg string) bool {
		if seg == "" {
			return false
		}
		if isAllDigits(seg) {
			// 十进制（含前导零：0177 按八进制解析，仍属 IP 字面量）
			return true
		}
		low := strings.ToLower(seg)
		if !strings.HasPrefix(low, "0x") || len(seg) <= 2 {
			return false
		}
		return isAllHex(low[2:])
	}
	if len(parts) == 1 {
		return segOK(parts[0])
	}
	if len(parts) > 4 {
		return false
	}
	for _, part := range parts {
		if !segOK(part) {
			return false
		}
	}
	return true
}

// isAllDigits 报告 s 是否全为十进制数字。
func isAllDigits(s string) bool {
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return len(s) > 0
}

// isAllHex 报告 s 是否全为十六进制数字。
func isAllHex(s string) bool {
	for _, r := range s {
		if !(r >= '0' && r <= '9' || r >= 'a' && r <= 'f') {
			return false
		}
	}
	return len(s) > 0
}
