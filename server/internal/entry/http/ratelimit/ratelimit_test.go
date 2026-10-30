package ratelimit

import (
	"net/netip"
	"testing"
	"time"
)

// 桶满即放行，配额耗尽即拒绝，并给出等待时间。
// 假时钟推进一个补充周期后恢复放行。
func TestAllowBurstThenRefill(t *testing.T) {
	l, err := NewThrottle(Quota{PerMinute: 60, Burst: 2})
	if err != nil {
		t.Fatalf("NewThrottle: %v", err)
	}
	now := time.Date(2026, 10, 30, 8, 0, 0, 0, time.UTC)
	l.now = func() time.Time { return now }

	for i := 0; i < 2; i++ {
		if ok, _ := l.Allow("ip"); !ok {
			t.Fatalf("第 %d 次必须放行", i+1)
		}
	}
	ok, wait := l.Allow("ip")
	if ok {
		t.Fatal("配额耗尽必须拒绝")
	}
	if wait <= 0 || wait > 2*time.Second {
		t.Fatalf("等待 = %v, want (0, 2s]", wait)
	}

	now = now.Add(time.Second)
	if ok, _ := l.Allow("ip"); !ok {
		t.Fatal("推进一个补充周期后必须放行")
	}
}

// 连续拒绝不放贷：每次拒绝都取消预留，等待窗口不随打点推远。
func TestRefusalDoesNotBorrow(t *testing.T) {
	l, err := NewThrottle(Quota{PerMinute: 60, Burst: 1})
	if err != nil {
		t.Fatalf("NewThrottle: %v", err)
	}
	now := time.Date(2026, 10, 30, 8, 0, 0, 0, time.UTC)
	l.now = func() time.Time { return now }
	if ok, _ := l.Allow("ip"); !ok {
		t.Fatal("首次必须放行")
	}
	_, first := l.Allow("ip")
	for i := 0; i < 5; i++ {
		if ok, _ := l.Allow("ip"); ok {
			t.Fatal("配额耗尽必须拒绝")
		}
	}
	_, last := l.Allow("ip")
	if last > first+100*time.Millisecond {
		t.Fatalf("等待被推远：first=%v last=%v", first, last)
	}
}

// 空闲桶按 2 倍充满时间回收；键数上限淘汰最久未见者。
func TestSweepAndKeyCap(t *testing.T) {
	l, err := NewThrottle(Quota{PerMinute: 60, Burst: 1})
	if err != nil {
		t.Fatalf("NewThrottle: %v", err)
	}
	base := time.Date(2026, 10, 30, 8, 0, 0, 0, time.UTC)
	now := base
	l.now = func() time.Time { return now }
	l.Allow("old")
	now = base.Add(l.ttl + time.Second)
	l.sweepLocked(now)
	if len(l.buckets) != 0 {
		t.Fatalf("空闲桶未回收: %d", len(l.buckets))
	}

	l.maxKeys = 2
	for _, key := range []string{"a", "b", "c"} {
		l.Allow(key)
	}
	if len(l.buckets) > l.maxKeys {
		t.Fatalf("键数 = %d, want <= %d", len(l.buckets), l.maxKeys)
	}
}

// nil 限流器视为不限流：未装配的路由与单元测试保持原行为。
func TestNilLimiterAllows(t *testing.T) {
	var l *Throttle
	if ok, _ := l.Allow("x"); !ok {
		t.Fatal("nil 限流器必须放行")
	}
	var g *Guard
	if ok, _ := g.AllowIP("127.0.0.1:1", nil); !ok {
		t.Fatal("nil Guard 必须放行")
	}
	if ok, _ := g.AllowAccount("u1"); !ok {
		t.Fatal("nil Guard 必须放行")
	}
}

// PerMinute 非正即配置错误：失败关闭，不静默全放行。
func TestNewLimiterRejectsNonPositive(t *testing.T) {
	for _, p := range []int{0, -1} {
		if _, err := NewThrottle(Quota{PerMinute: p}); err == nil {
			t.Fatalf("PerMinute=%d 必须拒绝", p)
		}
	}
}

func mustPrefixes(t *testing.T, raw string) []Prefix {
	t.Helper()
	out, err := ParseTrustedProxies(raw)
	if err != nil {
		t.Fatalf("ParseTrustedProxies(%q): %v", raw, err)
	}
	return out
}

// 无可信代理：XFF 一律忽略，只认直连对端。
func TestClientIPIgnoresForwardedWithoutTrust(t *testing.T) {
	got := ClientIP("203.0.113.9:443", []string{"198.51.100.7"}, mustPrefixes(t, ""))
	if got != "203.0.113.9" {
		t.Fatalf("client ip = %q, want 直连对端", got)
	}
}

// 对端可信：从右往左取第一个不在可信表内的地址。
func TestClientIPWalksTrustedChain(t *testing.T) {
	trusted := mustPrefixes(t, "10.0.0.0/8, 192.0.2.0/24")
	got := ClientIP("10.1.2.3:443", []string{"198.51.100.7, 192.0.2.10"}, trusted)
	if got != "198.51.100.7" {
		t.Fatalf("client ip = %q, want 198.51.100.7", got)
	}
}

// 单条头部里有多跳时必须取最右的不可信地址：
// 最左值由客户端写入，代理只在右侧追加。
func TestClientIPUsesRightmostUntrustedHop(t *testing.T) {
	trusted := mustPrefixes(t, "10.0.0.0/8")
	for _, forwarded := range [][]string{
		{"203.0.113.7, 198.51.100.9"},
		{"203.0.113.7", "198.51.100.9"},
	} {
		got := ClientIP("10.1.2.3:443", forwarded, trusted)
		if got != "198.51.100.9" {
			t.Fatalf("forwarded=%v client ip = %q, want 最右不可信跳 198.51.100.9", forwarded, got)
		}
	}
}

// 全部可信或头部畸形：回退直连对端，绝不返回空串。
func TestClientIPFallsBackToPeer(t *testing.T) {
	trusted := mustPrefixes(t, "10.0.0.0/8")
	for _, forwarded := range [][]string{
		{"10.9.9.9"},
		{"not-an-ip"},
		nil,
	} {
		if got := ClientIP("10.1.2.3:443", forwarded, trusted); got != "10.1.2.3" {
			t.Fatalf("forwarded=%v client ip = %q, want 对端", forwarded, got)
		}
	}
}

// 裸 IP 按单机网段解析；非法条目即错。
func TestParseTrustedProxies(t *testing.T) {
	got := mustPrefixes(t, "127.0.0.1, 10.0.0.0/8")
	if len(got) != 2 {
		t.Fatalf("前缀数 = %d, want 2", len(got))
	}
	if got[0] != netip.MustParsePrefix("127.0.0.1/32") {
		t.Fatalf("裸 IP = %v, want /32", got[0])
	}
	if _, err := ParseTrustedProxies("10.0.0.0/33"); err == nil {
		t.Fatal("非法 CIDR 必须报错")
	}
	if _, err := ParseTrustedProxies("foo"); err == nil {
		t.Fatal("非法地址必须报错")
	}
}

// 可信对端判定：只认直连对端，不看转发头。
func TestIsTrustedPeer(t *testing.T) {
	trusted := mustPrefixes(t, "10.0.0.0/8, 127.0.0.1")
	cases := []struct {
		remote string
		want   bool
	}{
		{"10.1.2.3:443", true},
		{"127.0.0.1:1234", true},
		{"203.0.113.9:443", false},
		{"not-an-address", false},
	}
	for _, c := range cases {
		if got := IsTrustedPeer(c.remote, trusted); got != c.want {
			t.Fatalf("IsTrustedPeer(%q) = %v, want %v", c.remote, got, c.want)
		}
	}
}
