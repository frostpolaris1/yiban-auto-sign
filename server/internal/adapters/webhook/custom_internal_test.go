package webhook

// 白盒测试：custom 出口的拨号地址改写（DNS rebinding TOCTOU 闭合点）。
// 判定与直连必须用同一组复核 IP；拨号地址的 host 换成复核 IP、端口保留。

import (
	"net"
	"testing"
)

// dialAddrs 把 host 换成复核 IP、端口保留；字面量目标原样返回。
func TestDialAddrsRewritesHostOnly(t *testing.T) {
	ips := []net.IPAddr{
		{IP: net.ParseIP("203.0.113.7")},
		{IP: net.ParseIP("2001:db8::1")},
	}
	got, err := dialAddrs("push.example.test:443", ips)
	if err != nil {
		t.Fatalf("dialAddrs: %v", err)
	}
	want := []string{"203.0.113.7:443", "[2001:db8::1]:443"}
	if len(got) != len(want) {
		t.Fatalf("candidates = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("candidate[%d] = %q, want %q", i, got[i], want[i])
		}
	}
	literal, err := dialAddrs("203.0.113.7:443", nil)
	if err != nil || literal[0] != "203.0.113.7:443" {
		t.Fatalf("literal passthrough = %v, %v", literal, err)
	}
	if _, err := dialAddrs("no-port", ips); err == nil {
		t.Fatal("address without a port must fail")
	}
}
