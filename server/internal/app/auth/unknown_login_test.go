package auth_test

// 未知邮箱登录审计抽样用例。
//
// # 功能
// 钉住「同一 IP 每分钟最多一条 auth.login_failed、每次仍 Warn、
// dummy KDF 不受影响、审计故障不改变 401」。
// 本文件**不**连数据库，**不**发 HTTP。
//
// # 口径
// 抽样只影响审计写入；日志与计时拉平每次都发生。

import (
	"context"
	"encoding/hex"
	"errors"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	appauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/auth"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
)

// ip_hash 是日志键面的一部分；这里写死键串以先钉住调用点。
const unknownLoginIPHashKey = "ip_hash"

// warnCapture 收集 Warn 及以上日志。用例只读快照，不并发写。
type warnCapture struct {
	mu      sync.Mutex
	records []slog.Record
}

func (c *warnCapture) Enabled(_ context.Context, level slog.Level) bool {
	return level >= slog.LevelWarn
}

func (c *warnCapture) Handle(_ context.Context, r slog.Record) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.records = append(c.records, r.Clone())
	return nil
}

func (c *warnCapture) WithAttrs([]slog.Attr) slog.Handler { return c }
func (c *warnCapture) WithGroup(string) slog.Handler      { return c }

func (c *warnCapture) snapshot() []slog.Record {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]slog.Record, len(c.records))
	copy(out, c.records)
	return out
}

func (c *warnCapture) count() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.records)
}

// attrOf 取一条日志记录里的字段值。
func attrOf(r slog.Record, key string) (any, bool) {
	var value any
	found := false
	r.Attrs(func(a slog.Attr) bool {
		if a.Key == key {
			value = a.Value.Any()
			found = true
			return false
		}
		return true
	})
	return value, found
}

// newSamplingHarness 构造带指定时钟与日志捕获的骨架。
func newSamplingHarness(t *testing.T, clk clock.Clock, capture *warnCapture) *harness {
	t.Helper()
	return newHarnessWith(t, func(d *appauth.Deps) {
		d.Clock = clk
		d.Logger = slog.New(capture)
	})
}

// loginFromIP 带客户端 IP 打一次未知邮箱登录。
func loginFromIP(ctx context.Context, h *harness, ip, email string) error {
	if ip != "" {
		ctx = coreauth.WithClientMeta(ctx, coreauth.ClientMeta{IP: ip})
	}
	_, err := h.svc.Login(ctx, email, "any-password-42")
	return err
}

// wantUnknownWarn 断言一条 Warn 带 ip_hash 与 reason=unknown_email，返回指纹。
func wantUnknownWarn(t *testing.T, r slog.Record) string {
	t.Helper()
	if r.Level != slog.LevelWarn {
		t.Fatalf("log level = %v, want warn", r.Level)
	}
	raw, ok := attrOf(r, unknownLoginIPHashKey)
	if !ok {
		t.Fatalf("warn has no %s: %v", unknownLoginIPHashKey, r)
	}
	fingerprint, ok := raw.(string)
	if !ok || fingerprint == "" {
		t.Fatalf("ip_hash = %v, want a non-empty string", raw)
	}
	reason, ok := attrOf(r, "reason")
	if !ok || reason != "unknown_email" {
		t.Fatalf("reason = %v, want unknown_email", reason)
	}
	if _, ok := attrOf(r, "email"); ok {
		t.Fatal("warn carries an email field")
	}
	if strings.Contains(r.Message, "@") || strings.Contains(fingerprint, "@") {
		t.Fatalf("warn leaks an address-like value: message=%q ip_hash=%q", r.Message, fingerprint)
	}
	return fingerprint
}

// wantUnknownLoginAudit 断言抽样审计的形状与无 PII 口径。
func wantUnknownLoginAudit(t *testing.T, entry coreaudit.Entry) {
	t.Helper()
	if entry.Action != coreaudit.ActionAuthLoginFailed {
		t.Fatalf("audit action = %q, want %q", entry.Action, coreaudit.ActionAuthLoginFailed)
	}
	if entry.ActorUserID != "" || entry.ActorRole != "" || entry.ActorLabel != "unknown" {
		t.Fatalf("actor = (%q, %q, %q), want empty/empty/unknown",
			entry.ActorUserID, entry.ActorRole, entry.ActorLabel)
	}
	if entry.TargetType != "user" || entry.TargetID != "" {
		t.Fatalf("target = (%q, %q), want user/empty", entry.TargetType, entry.TargetID)
	}
	if entry.Detail["reason"] != "unknown_email" {
		t.Fatalf("detail reason = %v, want unknown_email", entry.Detail["reason"])
	}
	if entry.Detail["count"] != 1 {
		t.Fatalf("detail count = %v, want 1", entry.Detail["count"])
	}
}

// isLowerHex 判断指纹是否为小写十六进制串。
func isLowerHex(s string) bool {
	if s == "" || strings.ToLower(s) != s {
		return false
	}
	_, err := hex.DecodeString(s)
	return err == nil
}

// 同一 IP 连打 3 次：3 次 401、3 次 dummy KDF、3 次 Warn，仅 1 条审计。
func TestUnknownEmailLoginAuditsOncePerIPPerMinute(t *testing.T) {
	clk := newMutableClock()
	capture := &warnCapture{}
	h := newSamplingHarness(t, clk, capture)
	ctx := t.Context()

	for i := 0; i < 3; i++ {
		if err := loginFromIP(ctx, h, "203.0.113.10", "nobody@example.com"); err != appauth.ErrInvalidCredentials {
			t.Fatalf("attempt %d = %v, want ErrInvalidCredentials", i+1, err)
		}
	}
	if got := h.kdf.verifies.Load(); got != 3 {
		t.Fatalf("dummy KDF verifies = %d, want 3", got)
	}
	if len(h.audits.entries) != 1 {
		t.Fatalf("audit rows = %d, want 1: %+v", len(h.audits.entries), h.audits.entries)
	}
	wantUnknownLoginAudit(t, h.audits.entries[0])

	warns := capture.snapshot()
	if len(warns) != 3 {
		t.Fatalf("warn rows = %d, want 3", len(warns))
	}
	hashes := make([]string, 0, len(warns))
	for _, r := range warns {
		hashes = append(hashes, wantUnknownWarn(t, r))
	}
	if hashes[0] != hashes[1] || hashes[1] != hashes[2] {
		t.Fatalf("ip_hash differs across the same IP: %v", hashes)
	}
	if len(hashes[0]) != 16 || !isLowerHex(hashes[0]) {
		t.Fatalf("ip_hash = %q, want 16 lowercase hex chars", hashes[0])
	}
	if strings.Contains(hashes[0], "203.0.113.10") {
		t.Fatalf("ip_hash contains the plaintext IP: %q", hashes[0])
	}
}

// 不同 IP 各自成桶：各写 1 条审计。
func TestUnknownEmailLoginAuditsPerDistinctIP(t *testing.T) {
	h := newSamplingHarness(t, newMutableClock(), &warnCapture{})
	ctx := t.Context()

	for _, ip := range []string{"203.0.113.11", "203.0.113.12"} {
		if err := loginFromIP(ctx, h, ip, "nobody@example.com"); err != appauth.ErrInvalidCredentials {
			t.Fatalf("login from %s = %v, want ErrInvalidCredentials", ip, err)
		}
	}
	if len(h.audits.entries) != 2 {
		t.Fatalf("audit rows = %d, want 2", len(h.audits.entries))
	}
	for _, entry := range h.audits.entries {
		wantUnknownLoginAudit(t, entry)
	}
}

// 同一 IP 推进 61 秒后可再写 1 条。
func TestUnknownEmailLoginResamplesAfterWindow(t *testing.T) {
	clk := newMutableClock()
	h := newSamplingHarness(t, clk, &warnCapture{})
	ctx := t.Context()

	if err := loginFromIP(ctx, h, "203.0.113.20", "nobody@example.com"); err != appauth.ErrInvalidCredentials {
		t.Fatalf("first login = %v, want ErrInvalidCredentials", err)
	}
	if len(h.audits.entries) != 1 {
		t.Fatalf("audit rows after first = %d, want 1", len(h.audits.entries))
	}

	clk.advance(time.Minute + time.Second)
	if err := loginFromIP(ctx, h, "203.0.113.20", "nobody@example.com"); err != appauth.ErrInvalidCredentials {
		t.Fatalf("second login = %v, want ErrInvalidCredentials", err)
	}
	if len(h.audits.entries) != 2 {
		t.Fatalf("audit rows after the window = %d, want 2", len(h.audits.entries))
	}
	for _, entry := range h.audits.entries {
		wantUnknownLoginAudit(t, entry)
	}
}

// 无 ClientMeta 的请求共用固定 fallback 桶，仍每分钟 1 条。
func TestUnknownEmailLoginWithoutMetaUsesFallbackBucket(t *testing.T) {
	capture := &warnCapture{}
	h := newSamplingHarness(t, newMutableClock(), capture)
	ctx := t.Context()

	for i := 0; i < 3; i++ {
		if _, err := h.svc.Login(ctx, "nobody@example.com", "any-password-42"); err != appauth.ErrInvalidCredentials {
			t.Fatalf("attempt %d = %v, want ErrInvalidCredentials", i+1, err)
		}
	}
	if len(h.audits.entries) != 1 {
		t.Fatalf("fallback audit rows = %d, want 1", len(h.audits.entries))
	}
	wantUnknownLoginAudit(t, h.audits.entries[0])
	warns := capture.snapshot()
	if len(warns) != 3 {
		t.Fatalf("warn rows = %d, want 3", len(warns))
	}
	for _, r := range warns {
		if got := wantUnknownWarn(t, r); got != "unknown" {
			t.Fatalf("fallback ip_hash = %q, want unknown", got)
		}
	}
}

// 抽样审计写失败第一次返回错误；同 IP 第二次被抑制，回到同形 401。
func TestUnknownEmailAuditFailureDoesNotChange401(t *testing.T) {
	h := newSamplingHarness(t, newMutableClock(), &warnCapture{})
	ctx := t.Context()
	auditErr := errors.New("audit store down")

	h.audits.err = auditErr
	if err := loginFromIP(ctx, h, "203.0.113.30", "nobody@example.com"); !errors.Is(err, auditErr) {
		t.Fatalf("first attempt = %v, want the audit error", err)
	}
	if err := loginFromIP(ctx, h, "203.0.113.30", "nobody@example.com"); err != appauth.ErrInvalidCredentials {
		t.Fatalf("suppressed attempt = %v, want ErrInvalidCredentials (not the audit error)", err)
	}
	if len(h.audits.entries) != 0 {
		t.Fatalf("audit rows = %d, want 0", len(h.audits.entries))
	}
}
