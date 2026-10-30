package auth_test

// 认证用例的单元测试。
//
// # 功能
// 用进程内替身钉住两条安全不变式：
// 登录失败不可区分；忘记口令不泄露账号存在性。
// 本文件**不**连数据库，**不**发 HTTP。
//
// # 口径
// 替身只实现端口方法，不模拟 SQL 形状。
// 真库行为由 cmd/server 的 e2e 负责。

import (
	"context"
	"io"
	"log/slog"
	"strings"
	"testing"
	"time"

	appauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/auth"
	appuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/user"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

var testClock = clock.Fixed{T: time.Date(2026, 10, 26, 9, 0, 0, 0, time.UTC)}

type fakeUsers struct {
	byEmail map[string]appuser.Record
	byID    map[string]appuser.Record
	updated []string
	rehash  []string
}

func newFakeUsers() *fakeUsers {
	return &fakeUsers{byEmail: map[string]appuser.Record{}, byID: map[string]appuser.Record{}}
}

func (f *fakeUsers) Create(context.Context, appuser.New, int) (appuser.Record, error) {
	return appuser.Record{}, errs.ErrNotFound
}

func (f *fakeUsers) GetByID(_ context.Context, id string) (appuser.Record, error) {
	if rec, ok := f.byID[id]; ok {
		return rec, nil
	}
	return appuser.Record{}, errs.ErrNotFound
}

func (f *fakeUsers) GetByEmail(_ context.Context, email string) (appuser.Record, error) {
	if rec, ok := f.byEmail[strings.ToLower(email)]; ok {
		return rec, nil
	}
	return appuser.Record{}, errs.ErrNotFound
}

func (f *fakeUsers) List(context.Context, coreauth.Role, coreauth.Status, string, string, int) ([]appuser.Record, string, error) {
	return nil, "", nil
}

func (f *fakeUsers) UpdatePassword(_ context.Context, id, hash string, _ bool, _ time.Time) error {
	f.updated = append(f.updated, id+"="+hash)
	return nil
}

func (f *fakeUsers) RehashPassword(_ context.Context, id, hash string) error {
	f.rehash = append(f.rehash, id+"="+hash)
	return nil
}

func (f *fakeUsers) SetStatus(context.Context, string, coreauth.Status, time.Time) error { return nil }
func (f *fakeUsers) MarkLogin(context.Context, string, time.Time) error                  { return nil }
func (f *fakeUsers) SoftDelete(context.Context, string, string, time.Time) error         { return nil }

type fakeSessions struct {
	issued        []coreauth.User
	revokedAll    []string
	revokedOthers []string
}

func (f *fakeSessions) Issue(_ context.Context, user coreauth.User) (coreauth.Issued, error) {
	f.issued = append(f.issued, user)
	return coreauth.Issued{
		Token:   "token",
		Session: coreauth.Session{User: user, CSRFToken: "csrf"},
	}, nil
}

func (f *fakeSessions) Resolve(context.Context, string) (coreauth.Session, error) {
	return coreauth.Session{}, coreauth.ErrNoSession
}

func (f *fakeSessions) Revoke(context.Context, string) error { return nil }

func (f *fakeSessions) RevokeAllForUser(_ context.Context, userID string) error {
	f.revokedAll = append(f.revokedAll, userID)
	return nil
}

func (f *fakeSessions) RevokeOthers(_ context.Context, userID string, _ coreauth.TokenHash, _ time.Time) error {
	f.revokedOthers = append(f.revokedOthers, userID)
	return nil
}

type fakeResets struct {
	created []coreauth.NewPasswordReset
	next    *coreauth.PasswordReset
	consume int
	peek    int
}

func (f *fakeResets) Create(_ context.Context, in coreauth.NewPasswordReset) (coreauth.PasswordReset, error) {
	f.created = append(f.created, in)
	return coreauth.PasswordReset{UserID: in.UserID, TokenHash: in.TokenHash}, nil
}

func (f *fakeResets) Peek(context.Context, coreauth.TokenHash, time.Time) (coreauth.PasswordReset, error) {
	f.peek++
	if f.next == nil {
		return coreauth.PasswordReset{}, errs.ErrNotFound
	}
	return *f.next, nil
}

func (f *fakeResets) Consume(context.Context, coreauth.TokenHash, time.Time) (coreauth.PasswordReset, error) {
	f.consume++
	if f.next == nil {
		return coreauth.PasswordReset{}, errs.ErrNotFound
	}
	return *f.next, nil
}

type fakeAudits struct {
	actions []coreaudit.Action
}

func (f *fakeAudits) Append(_ context.Context, in coreaudit.Entry) (coreaudit.Row, error) {
	f.actions = append(f.actions, in.Action)
	return coreaudit.Row{}, nil
}

func (f *fakeAudits) Verify(context.Context, int64, int64) (coreaudit.Report, error) {
	return coreaudit.Report{}, nil
}

type fakeTx struct{}

func (fakeTx) InTx(ctx context.Context, fn func(context.Context) error) error { return fn(ctx) }

type harness struct {
	users    *fakeUsers
	sessions *fakeSessions
	resets   *fakeResets
	audits   *fakeAudits
	kdf      *countingKDF
	svc      *appauth.Service
}

// countingKDF 计数哈希与校验次数，行为与 PlainHasher 相同。
// 用于钉住「垃圾重置令牌不付 Argon2」与「有效令牌只哈希一次」。
type countingKDF struct {
	hashes   int
	verifies int
}

func (c *countingKDF) Hash(ctx context.Context, password string) (string, error) {
	c.hashes++
	return coreauth.PlainHasher{}.Hash(ctx, password)
}

func (c *countingKDF) Verify(ctx context.Context, password, encoded string) (coreauth.PasswordCheck, error) {
	c.verifies++
	return coreauth.PlainHasher{}.Verify(ctx, password, encoded)
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	h := &harness{
		users:    newFakeUsers(),
		sessions: &fakeSessions{},
		resets:   &fakeResets{},
		audits:   &fakeAudits{},
		kdf:      &countingKDF{},
	}
	svc, err := appauth.NewService(appauth.Deps{
		Users:         h.users,
		Sessions:      h.sessions,
		OtherSessions: h.sessions,
		Resets:        h.resets,
		Audits:        h.audits,
		Tx:            fakeTx{},
		Clock:         testClock,
		KDF:           h.kdf,
		// 本套不测日志；未配置投递的警告不污染测试输出。
		Logger: slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	h.svc = svc
	return h
}

// 登录失败的两条路径必须给出同一个错误。
func TestLoginFailureIsIndistinguishable(t *testing.T) {
	h := newHarness(t)
	hash, err := coreauth.HashPassword("right-password")
	if err != nil {
		t.Fatalf("HashPassword: %v", err)
	}
	h.users.byEmail["known@example.com"] = appuser.Record{
		ID: "u1", Email: "known@example.com", PasswordHash: hash,
		Role: coreauth.RoleUser, Status: coreauth.StatusActive,
	}
	h.users.byID["u1"] = h.users.byEmail["known@example.com"]

	wrong, err := h.svc.Login(t.Context(), "known@example.com", "wrong-password")
	if err != appauth.ErrInvalidCredentials {
		t.Fatalf("wrong password error = %v, want ErrInvalidCredentials", err)
	}
	unknown, err := h.svc.Login(t.Context(), "nobody@example.com", "wrong-password")
	if err != appauth.ErrInvalidCredentials {
		t.Fatalf("unknown user error = %v, want ErrInvalidCredentials", err)
	}
	if wrong != unknown {
		t.Fatal("the two login failures are not the same value")
	}
	if len(h.sessions.issued) != 0 {
		t.Fatalf("failed logins issued %d sessions", len(h.sessions.issued))
	}
	if len(h.audits.actions) != 2 || h.audits.actions[0] != coreaudit.ActionAuthLoginFailed {
		t.Fatalf("audit actions = %v, want two auth.login_failed", h.audits.actions)
	}
}

// 旧哈希登录成功后必须要求重哈希，且不改 password_changed_at 路径。
func TestLoginMarksLegacyHashForRehash(t *testing.T) {
	h := newHarness(t)
	h.users.byEmail["legacy@example.com"] = appuser.Record{
		ID: "u2", Email: "legacy@example.com",
		PasswordHash: "scrypt:16384:8:1$testsalt12345678$06b91ed25cd188bc0738dd4d8cd0721bd4102bd1828e3d3737652fb6150cebfa636001232480be38c4877be172c5fc282cda669f9df0dc696154129e8153ce98",
		Role:         coreauth.RoleUser, Status: coreauth.StatusActive,
	}
	issued, err := h.svc.Login(t.Context(), "legacy@example.com", "legacy-pass")
	if err != nil {
		t.Fatalf("Login: %v", err)
	}
	if issued.Token == "" || issued.User.ID != "u2" {
		t.Fatalf("issued = %+v", issued)
	}
	if len(h.users.rehash) != 1 {
		t.Fatalf("rehash calls = %v, want 1", h.users.rehash)
	}
	if !strings.Contains(h.users.rehash[0], "$argon2id$") {
		t.Fatalf("rehash stored %q, want argon2id", h.users.rehash[0])
	}
	if len(h.users.updated) != 0 {
		t.Fatalf("transparent upgrade used UpdatePassword: %v", h.users.updated)
	}
}

// 忘记口令对未知邮箱返回成功，且不写令牌行。
func TestForgotPasswordHidesUnknownEmail(t *testing.T) {
	h := newHarness(t)
	if err := h.svc.ForgotPassword(t.Context(), "absent@example.com"); err != nil {
		t.Fatalf("ForgotPassword(unknown): %v", err)
	}
	if len(h.resets.created) != 0 {
		t.Fatalf("unknown email created %d reset rows", len(h.resets.created))
	}
	if len(h.audits.actions) != 0 {
		t.Fatalf("forgot-password wrote audit actions %v; the action is not in the vocabulary", h.audits.actions)
	}
}

func TestForgotPasswordStoresOnlyTheHash(t *testing.T) {
	h := newHarness(t)
	h.users.byEmail["known@example.com"] = appuser.Record{
		ID: "u3", Email: "known@example.com", Role: coreauth.RoleUser, Status: coreauth.StatusActive,
	}
	if err := h.svc.ForgotPassword(t.Context(), "Known@Example.com"); err != nil {
		t.Fatalf("ForgotPassword: %v", err)
	}
	if len(h.resets.created) != 1 {
		t.Fatalf("created %d reset rows, want 1", len(h.resets.created))
	}
	in := h.resets.created[0]
	if in.UserID != "u3" {
		t.Fatalf("reset user = %q", in.UserID)
	}
	if in.TokenHash.IsZero() {
		t.Fatal("reset row has a zero token hash")
	}
	if !in.ExpiresAt.After(in.Now) {
		t.Fatalf("expiry %s is not after now %s", in.ExpiresAt, in.Now)
	}
}

// median 返回样本中位数。样本数取奇数，避免两值平均掩盖单边漂移。
func median(samples []time.Duration) time.Duration {
	sorted := append([]time.Duration(nil), samples...)
	for i := 1; i < len(sorted); i++ {
		for j := i; j > 0 && sorted[j] < sorted[j-1]; j-- {
			sorted[j], sorted[j-1] = sorted[j-1], sorted[j]
		}
	}
	return sorted[len(sorted)/2]
}

// 登录失败的两条路径必须同量级。
//
// 阈值 [1/3, 3]。本机实测（2026-10-26，WSL，fake 仓储，11 轮中位数）：
// 已知用户口令错 17.2 ms，未知用户 16.2 ms，比值 1.06。
// 突变实测：删掉未知路径的 verifyDummyPassword 后，未知路径降到微秒级，
// 比值远超 3（突变记录见 W2B 修复报告）。
func TestLoginFailureTimingIsLeveled(t *testing.T) {
	h := newHarness(t)
	hash, err := coreauth.HashPassword("right-password")
	if err != nil {
		t.Fatalf("HashPassword: %v", err)
	}
	h.users.byEmail["known@example.com"] = appuser.Record{
		ID: "u5", Email: "known@example.com", PasswordHash: hash,
		Role: coreauth.RoleUser, Status: coreauth.StatusActive,
	}
	// 预热：dummyOnce 首次调用要额外算一次哈希。
	_, _ = h.svc.Login(t.Context(), "known@example.com", "wrong-password")
	_, _ = h.svc.Login(t.Context(), "nobody@example.com", "wrong-password")

	const rounds = 11
	known := make([]time.Duration, 0, rounds)
	unknown := make([]time.Duration, 0, rounds)
	for i := 0; i < rounds; i++ {
		start := time.Now()
		_, _ = h.svc.Login(t.Context(), "known@example.com", "wrong-password")
		known = append(known, time.Since(start))

		start = time.Now()
		_, _ = h.svc.Login(t.Context(), "nobody@example.com", "wrong-password")
		unknown = append(unknown, time.Since(start))
	}

	knownMedian := median(known)
	unknownMedian := median(unknown)
	ratio := float64(knownMedian) / float64(unknownMedian)
	t.Logf("login failure medians: known=%s unknown=%s ratio=%.2f", knownMedian, unknownMedian, ratio)
	if ratio < 1.0/3.0 || ratio > 3.0 {
		t.Fatalf("login failure timing ratio = %.2f (known %s, unknown %s), want [1/3, 3]",
			ratio, knownMedian, unknownMedian)
	}
}

// 忘记口令的两条路径必须同量级。
//
// 阈值 [1/2, 2]。本机实测（2026-10-26，WSL，fake 仓储，11 轮中位数）：
// 已知邮箱 16.4 ms，未知邮箱 16.5 ms，比值 0.99。
// 突变实测：删掉未知路径的垫时后，未知路径降到微秒级，比值远超 2。
func TestForgotPasswordTimingIsLeveled(t *testing.T) {
	h := newHarness(t)
	h.users.byEmail["known@example.com"] = appuser.Record{
		ID: "u6", Email: "known@example.com", Role: coreauth.RoleUser, Status: coreauth.StatusActive,
	}
	// 预热：dummyOnce 首次调用要额外算一次哈希。
	_ = h.svc.ForgotPassword(t.Context(), "known@example.com")
	_ = h.svc.ForgotPassword(t.Context(), "absent@example.com")

	const rounds = 11
	known := make([]time.Duration, 0, rounds)
	unknown := make([]time.Duration, 0, rounds)
	for i := 0; i < rounds; i++ {
		start := time.Now()
		_ = h.svc.ForgotPassword(t.Context(), "known@example.com")
		known = append(known, time.Since(start))

		start = time.Now()
		_ = h.svc.ForgotPassword(t.Context(), "absent@example.com")
		unknown = append(unknown, time.Since(start))
	}

	knownMedian := median(known)
	unknownMedian := median(unknown)
	ratio := float64(knownMedian) / float64(unknownMedian)
	t.Logf("forgot-password medians: known=%s unknown=%s ratio=%.2f", knownMedian, unknownMedian, ratio)
	if ratio < 0.5 || ratio > 2.0 {
		t.Fatalf("forgot-password timing ratio = %.2f (known %s, unknown %s), want [1/2, 2]",
			ratio, knownMedian, unknownMedian)
	}
}

func TestResetPasswordRejectsUnknownToken(t *testing.T) {
	h := newHarness(t)
	err := h.svc.ResetPassword(t.Context(), "no-such-token", "new-password")
	if err != appauth.ErrInvalidResetToken {
		t.Fatalf("ResetPassword(unknown) = %v, want ErrInvalidResetToken", err)
	}
	if len(h.users.updated) != 0 {
		t.Fatal("an unknown token changed a password")
	}
	// 先验令牌再算哈希：垃圾令牌不得触发任何 Argon2 成本。
	if h.kdf.hashes != 0 {
		t.Fatalf("垃圾令牌触发了 %d 次哈希，want 0", h.kdf.hashes)
	}
	if h.resets.peek != 1 || h.resets.consume != 0 {
		t.Fatalf("peek=%d consume=%d, want 1/0", h.resets.peek, h.resets.consume)
	}
}

func TestResetPasswordRevokesAllSessions(t *testing.T) {
	h := newHarness(t)
	h.resets.next = &coreauth.PasswordReset{UserID: "u4"}
	h.users.byID["u4"] = appuser.Record{
		ID: "u4", Email: "reset@example.com", Role: coreauth.RoleUser, Status: coreauth.StatusActive,
	}
	if err := h.svc.ResetPassword(t.Context(), "good-token", "new-password"); err != nil {
		t.Fatalf("ResetPassword: %v", err)
	}
	if h.kdf.hashes != 1 {
		t.Fatalf("有效令牌哈希次数 = %d, want 1", h.kdf.hashes)
	}
	if len(h.users.updated) != 1 || !strings.Contains(h.users.updated[0], "$argon2id$") {
		t.Fatalf("password updates = %v, want one argon2id write", h.users.updated)
	}
	if len(h.sessions.revokedAll) != 1 || h.sessions.revokedAll[0] != "u4" {
		t.Fatalf("revoked all = %v, want [u4]", h.sessions.revokedAll)
	}
	if len(h.audits.actions) != 1 || h.audits.actions[0] != coreaudit.ActionAuthPasswordReset {
		t.Fatalf("audit actions = %v, want auth.password_reset", h.audits.actions)
	}
}
