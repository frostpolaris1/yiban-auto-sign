package yiban

import "testing"

// allowedUploadURL 的白盒表驱动判据（P3-1）。
//
// 行为侧「真接线、零 PUT」判据见 upload_ssrf_test.go；
// 这里只钉地址分类的边界，避免每条边界都起一台假上游。
func TestAllowedUploadURL(t *testing.T) {
	cases := []struct {
		name          string
		raw           string
		allowLoopback bool
		want          bool
	}{
		// 生产：https 公网字面量放行（含域名、端口、公网 IP、IPv6）。
		{"https-domain", "https://upload.example.com/upload/x.jpg", false, true},
		{"https-domain-port", "https://cdn.example.com:8443/upload/x.jpg", false, true},
		{"https-domain-trailing-dot", "https://upload.example.com./x.jpg", false, true},
		{"https-fragment", "https://upload.example.com/x.jpg#frag", false, true},
		{"https-public-ipv4", "https://203.0.113.10/x.jpg", false, true},
		{"https-public-ipv6", "https://[2001:4860:4860::8888]/x.jpg", false, true},
		{"https-uppercase-scheme-host", "HTTPS://Upload.Example.COM/x.jpg", false, true},

		// 生产：方案必须是 https。
		{"plain-http-domain", "http://example.com/x.jpg", false, false},
		{"plain-http-loopback", "http://127.0.0.1:8080/x.jpg", false, false},
		{"ftp-scheme", "ftp://example.com/x.jpg", false, false},
		{"relative-url", "//example.com/x.jpg", false, false},
		{"missing-scheme", "example.com/x.jpg", false, false},

		// 生产：localhost 与 *.localhost（含大小写与末尾点）。
		{"localhost", "https://localhost/x.jpg", false, false},
		{"localhost-uppercase", "https://LOCALHOST/x.jpg", false, false},
		{"localhost-trailing-dot", "https://localhost./x.jpg", false, false},
		{"subdomain-localhost", "https://upload.localhost/x.jpg", false, false},
		{"deep-subdomain-localhost", "https://a.b.localhost./x.jpg", false, false},

		// 生产：环回/私网/链路本地/未指定/组播的 IP 字面量。
		{"loopback-ipv4", "https://127.0.0.1/x.jpg", false, false},
		{"loopback-ipv4-trailing-dot", "https://127.0.0.1./x.jpg", false, false},
		{"loopback-ipv6", "https://[::1]/x.jpg", false, false},
		{"loopback-mapped-ipv6", "https://[::ffff:127.0.0.1]/x.jpg", false, false},
		// zone-id（RFC 6874）绕开 net.ParseIP：Hostname() 是 "::1%lo" 这类带 % 的串。
		{"loopback-ipv6-zone", "https://[::1%25lo]/x.jpg", false, false},
		{"loopback-mapped-ipv6-zone", "https://[::ffff:127.0.0.1%25lo]/x.jpg", false, false},
		{"link-local-ipv6-zone", "https://[fe80::1%25eth0]/x.jpg", false, false},
		{"unspecified-ipv6-zone", "https://[::%25lo]/x.jpg", false, false},
		{"private-ipv6-zone", "https://[fd00::1%25eth0]/x.jpg", false, false},
		// 多个末尾点不能只归一一个，否则 127.0.0.1.. 会漏到 DNS 分支。
		{"loopback-ipv4-multi-dot", "https://127.0.0.1../x.jpg", false, false},
		{"private-10", "https://10.1.2.3/x.jpg", false, false},
		{"private-172", "https://172.16.5.4/x.jpg", false, false},
		{"private-192", "https://192.168.1.10/x.jpg", false, false},
		{"private-ipv6-ula", "https://[fc00::1]/x.jpg", false, false},
		{"link-local-ipv4", "https://169.254.169.254/latest/meta-data/", false, false},
		{"link-local-ipv6", "https://[fe80::1]/x.jpg", false, false},
		{"unspecified-ipv4", "https://0.0.0.0/x.jpg", false, false},
		{"unspecified-ipv6", "https://[::]/x.jpg", false, false},
		{"multicast-ipv4", "https://239.1.2.3/x.jpg", false, false},
		{"multicast-ipv6", "https://[ff02::1]/x.jpg", false, false},
		{"userinfo-hides-loopback", "https://user:pw@127.0.0.1/x.jpg", false, false},

		// 解析失败 / 无主机。
		{"empty", "", false, false},
		{"garbage", "https://[::1", false, false},
		{"no-host", "https:///x.jpg", false, false},
		{"empty-host", "https://", false, false},

		// 测试钩子：只额外放行 http 的 127.0.0.1 / ::1。
		{"hook-http-loopback", "http://127.0.0.1:8080/upload/x.jpg", true, true},
		{"hook-http-loopback-no-port", "http://127.0.0.1/upload/x.jpg", true, true},
		{"hook-http-ipv6-loopback", "http://[::1]:8080/upload/x.jpg", true, true},
		{"hook-http-localhost", "http://localhost:8080/upload/x.jpg", true, false},
		{"hook-http-other-loopback", "http://127.0.0.2:8080/upload/x.jpg", true, false},
		{"hook-https-loopback", "https://127.0.0.1/x.jpg", true, false},
		{"hook-http-private", "http://10.1.2.3/x.jpg", true, false},
		{"hook-http-domain", "http://example.com/x.jpg", true, false},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, reason := allowedUploadURL(tc.raw, tc.allowLoopback)
			if got != tc.want {
				t.Fatalf("allowedUploadURL(%q, %v) = %v (%q), want %v",
					tc.raw, tc.allowLoopback, got, reason, tc.want)
			}
			if tc.want && reason != "" {
				t.Fatalf("放行时不应带拒绝原因：%q", reason)
			}
			if !tc.want && reason == "" {
				t.Fatalf("拒绝时必须给出原因")
			}
		})
	}
}
