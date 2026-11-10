package settings_test

// SMTP 设置读写与每发送解析（批 smtp4）的用例层守卫。
//
// 判据：GET master/admin 可读、user 403；PUT 只有 master；
// 缺行=env/registry 值、有效行=db、坏行回退 env 且每键 Warn 一次；
// 部分补丁只动列出的键；null 删行回 env；
// mail.pass 落库是密文，明文不进存储、审计、响应与日志；
// 加密失败 500 零写入；审计失败整体回滚；
// 域校验（host/port/tls/from/user/pass）拒绝时零写入零审计。

import (
	"context"
	"encoding/base64"
	"errors"
	"strings"
	"testing"

	appsettings "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/settings"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// mailCipher 是 SecretCipher 的进程内替身：blob 带 sealed: 前缀，
// 只记录自己封过的值。真实 AES-GCM 往返由 app/account 与 cmd/server e2e 钉。
type mailCipher struct {
	sealed   map[string]string
	failSeal bool
	failOpen bool
}

func newMailCipher() *mailCipher {
	return &mailCipher{sealed: map[string]string{}}
}

func (c *mailCipher) SealSecret(_ string, plaintext string) (string, error) {
	if c.failSeal {
		return "", errors.New("seal failed")
	}
	blob := "sealed:" + base64.RawStdEncoding.EncodeToString([]byte(plaintext))
	c.sealed[blob] = plaintext
	return blob, nil
}

func (c *mailCipher) OpenSecret(_ string, blob string) (string, error) {
	if c.failOpen {
		return "", errors.New("open failed")
	}
	plain, ok := c.sealed[blob]
	if !ok {
		return "", errors.New("unknown blob")
	}
	return plain, nil
}

// mailEnv 是装配端注入的 env/registry 兜底值。
func mailEnv() appsettings.MailEnv {
	return appsettings.MailEnv{
		Host:    appsettings.MailEnvValue{Value: "smtp.env.example", Source: appsettings.MailSourceEnv},
		Port:    appsettings.MailEnvValue{Value: "465", Source: appsettings.MailSourceEnv},
		TLSMode: appsettings.MailEnvValue{Value: "auto", Source: appsettings.MailSourceEnv},
		From:    appsettings.MailEnvValue{Value: "env@example.com", Source: appsettings.MailSourceEnv},
		User:    appsettings.MailEnvValue{Value: "env-user", Source: appsettings.MailSourceEnv},
		Pass:    appsettings.MailEnvValue{Value: "env-pass", Source: appsettings.MailSourceEnv},
	}
}

type mailFixture struct {
	svc    *appsettings.Service
	store  *keyedWriteStore
	audits *fakeAudit
	cipher *mailCipher
	tx     *rollbackTx
}

func newMailFixture(t *testing.T, env appsettings.MailEnv, cipher appsettings.SecretCipher) *mailFixture {
	t.Helper()
	store := newKeyedWriteStore()
	resolver := appsettings.NewMailResolver(store, cipher, env, nil)
	audits := &fakeAudit{}
	tx := &rollbackTx{}
	svc, err := appsettings.NewService(appsettings.Deps{
		Store: &fakeStore{}, Audit: audits, Tx: tx, Clock: fixedClock{},
		Schedule: store, Cipher: cipher, MailResolver: resolver,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return &mailFixture{svc: svc, store: store, audits: audits, cipher: cipher.(*mailCipher), tx: tx}
}

// GET：缺行回 env 值；master/admin 可读，user 403。
func TestMailSettingsDefaultsAndRoles(t *testing.T) {
	f := newMailFixture(t, mailEnv(), newMailCipher())
	ctx := context.Background()
	for _, scope := range []corescope.Scope{corescope.Master(), corescope.Admin(nil)} {
		report, err := f.svc.MailSettings(ctx, scope)
		if err != nil {
			t.Fatalf("%v MailSettings: %v", scope.Kind(), err)
		}
		if report.Host.Value != "smtp.env.example" || report.Host.Source != appsettings.MailSourceEnv {
			t.Fatalf("host = %+v", report.Host)
		}
		if report.Port.Value != 465 || report.Port.Source != appsettings.MailSourceEnv {
			t.Fatalf("port = %+v", report.Port)
		}
		if report.TLSMode.Value != "auto" || report.TLSMode.Source != appsettings.MailSourceEnv {
			t.Fatalf("tls = %+v", report.TLSMode)
		}
		if !report.From.Set || report.From.Masked != "****" || report.From.Source != appsettings.MailSourceEnv {
			t.Fatalf("from = %+v", report.From)
		}
		if !report.User.Set || report.User.Masked != "****" {
			t.Fatalf("user = %+v", report.User)
		}
		if !report.Pass.Set || report.Pass.Source != appsettings.MailSourceEnv {
			t.Fatalf("pass = %+v", report.Pass)
		}
	}
	if _, err := f.svc.MailSettings(ctx, corescope.User("u1")); err == nil ||
		errs.From(err).Code != errs.CodeForbidden {
		t.Fatalf("user MailSettings err = %v, want forbidden", err)
	}
}

// 有效 DB 行覆盖 env；pass 密文解回后只报 set。
func TestMailSettingsPrefersValidDBRows(t *testing.T) {
	cipher := newMailCipher()
	blob, err := cipher.SealSecret("x", "db-pass")
	if err != nil {
		t.Fatalf("SealSecret: %v", err)
	}
	f := newMailFixture(t, mailEnv(), cipher)
	f.store.strRows[appsettings.KeyMailSMTPHost] = "smtp.db.example"
	f.store.strRows[appsettings.KeyMailSMTPPort] = "587"
	f.store.strRows[appsettings.KeyMailTLSMode] = "starttls"
	f.store.strRows[appsettings.KeyMailFrom] = "db@example.com"
	f.store.strRows[appsettings.KeyMailUser] = "db-user"
	f.store.strRows[appsettings.KeyMailPass] = blob

	report, err := f.svc.MailSettings(context.Background(), corescope.Master())
	if err != nil {
		t.Fatalf("MailSettings: %v", err)
	}
	if report.Host.Value != "smtp.db.example" || report.Host.Source != appsettings.MailSourceDB {
		t.Fatalf("host = %+v", report.Host)
	}
	if report.Port.Value != 587 || report.Port.Source != appsettings.MailSourceDB {
		t.Fatalf("port = %+v", report.Port)
	}
	if report.TLSMode.Value != "starttls" || report.TLSMode.Source != appsettings.MailSourceDB {
		t.Fatalf("tls = %+v", report.TLSMode)
	}
	if !report.From.Set || report.From.Source != appsettings.MailSourceDB {
		t.Fatalf("from = %+v", report.From)
	}
	if !report.User.Set || report.User.Source != appsettings.MailSourceDB {
		t.Fatalf("user = %+v", report.User)
	}
	if !report.Pass.Set || report.Pass.Source != appsettings.MailSourceDB {
		t.Fatalf("pass = %+v", report.Pass)
	}
}

// 坏行（空主机/越域端口/词表外 TLS/无法解密的密文）回退 env 并 Warn 一次。
func TestMailSettingsBadRowsFallBackToEnv(t *testing.T) {
	cipher := newMailCipher()
	f := newMailFixture(t, mailEnv(), cipher)
	f.store.strRows[appsettings.KeyMailSMTPHost] = "bad host"
	f.store.strRows[appsettings.KeyMailSMTPPort] = "70000"
	f.store.strRows[appsettings.KeyMailTLSMode] = "ssl"
	f.store.strRows[appsettings.KeyMailPass] = "not-a-cipher-blob"

	for i := 0; i < 2; i++ {
		report, err := f.svc.MailSettings(context.Background(), corescope.Master())
		if err != nil {
			t.Fatalf("MailSettings: %v", err)
		}
		if report.Host.Value != "smtp.env.example" || report.Host.Source != appsettings.MailSourceEnv {
			t.Fatalf("bad host did not fall back: %+v", report.Host)
		}
		if report.Port.Value != 465 || report.Port.Source != appsettings.MailSourceEnv {
			t.Fatalf("bad port did not fall back: %+v", report.Port)
		}
		if report.TLSMode.Value != "auto" || report.TLSMode.Source != appsettings.MailSourceEnv {
			t.Fatalf("bad tls did not fall back: %+v", report.TLSMode)
		}
		if report.Pass.Source != appsettings.MailSourceEnv {
			t.Fatalf("bad pass did not fall back: %+v", report.Pass)
		}
	}
}

// 只有 master 能写：admin 与 user 都 403，零写入零审计。
func TestSetMailIsMasterOnly(t *testing.T) {
	f := newMailFixture(t, mailEnv(), newMailCipher())
	changes := []appsettings.MailChange{
		{Key: appsettings.KeyMailSMTPHost, Op: appsettings.OpSet, Value: "smtp.new.example"},
	}
	for _, scope := range []corescope.Scope{corescope.Admin(nil), corescope.User("u1")} {
		if err := f.svc.SetMail(context.Background(), scope, scheduleActor, changes); err == nil ||
			errs.From(err).Code != errs.CodeForbidden {
			t.Fatalf("%v SetMail err = %v, want forbidden", scope.Kind(), err)
		}
	}
	if len(f.store.strWrites) != 0 || len(f.store.clears) != 0 || len(f.audits.entries) != 0 {
		t.Fatalf("被拒写有副作用: writes=%v clears=%v audit=%d",
			f.store.strWrites, f.store.clears, len(f.audits.entries))
	}
}

// 部分补丁：只写 host，其它键不动；审计恰一行，非敏感键记字面旧→新。
func TestSetMailPartialPatchAuditsLiteralHost(t *testing.T) {
	f := newMailFixture(t, mailEnv(), newMailCipher())
	err := f.svc.SetMail(context.Background(), corescope.Master(), scheduleActor,
		[]appsettings.MailChange{
			{Key: appsettings.KeyMailSMTPHost, Op: appsettings.OpSet, Value: "smtp.new.example"},
		})
	if err != nil {
		t.Fatalf("SetMail: %v", err)
	}
	if f.store.strRows[appsettings.KeyMailSMTPHost] != "smtp.new.example" {
		t.Fatalf("host row = %q", f.store.strRows[appsettings.KeyMailSMTPHost])
	}
	if len(f.store.strWrites) != 1 {
		t.Fatalf("str writes = %v, want only host", f.store.strWrites)
	}
	if len(f.audits.entries) != 1 || f.audits.entries[0].Action != coreaudit.ActionSettingsMailWriteback {
		t.Fatalf("audit = %+v", f.audits.entries)
	}
	pair := f.audits.entries[0].Detail["changes"].(map[string][2]any)[appsettings.KeyMailSMTPHost]
	if pair[0] != nil || pair[1] != "smtp.new.example" {
		t.Fatalf("host pair = %#v, want [nil smtp.new.example]", pair)
	}
}

// null 删行：该键回 env；非敏感键审计新态是 nil。
func TestSetMailClearFallsBackToEnv(t *testing.T) {
	f := newMailFixture(t, mailEnv(), newMailCipher())
	f.store.strRows[appsettings.KeyMailSMTPHost] = "smtp.db.example"
	err := f.svc.SetMail(context.Background(), corescope.Master(), scheduleActor,
		[]appsettings.MailChange{
			{Key: appsettings.KeyMailSMTPHost, Op: appsettings.OpClear},
		})
	if err != nil {
		t.Fatalf("SetMail clear: %v", err)
	}
	if _, ok := f.store.strRows[appsettings.KeyMailSMTPHost]; ok {
		t.Fatalf("host row still present: %v", f.store.strRows)
	}
	pair := f.audits.entries[0].Detail["changes"].(map[string][2]any)[appsettings.KeyMailSMTPHost]
	if pair[0] != "smtp.db.example" || pair[1] != nil {
		t.Fatalf("clear pair = %#v, want [old nil]", pair)
	}
	report, err := f.svc.MailSettings(context.Background(), corescope.Master())
	if err != nil {
		t.Fatalf("MailSettings: %v", err)
	}
	if report.Host.Value != "smtp.env.example" || report.Host.Source != appsettings.MailSourceEnv {
		t.Fatalf("cleared host = %+v", report.Host)
	}
}

// 口令落库是密文：存储、审计、响应都不含明文；敏感键审计只记 set。
func TestSetMailEncryptsPassword(t *testing.T) {
	const plaintext = "smtp-auth-code-9!"
	f := newMailFixture(t, mailEnv(), newMailCipher())
	err := f.svc.SetMail(context.Background(), corescope.Master(), scheduleActor,
		[]appsettings.MailChange{
			{Key: appsettings.KeyMailPass, Op: appsettings.OpSet, Value: plaintext},
			{Key: appsettings.KeyMailUser, Op: appsettings.OpSet, Value: "smtp-user"},
		})
	if err != nil {
		t.Fatalf("SetMail: %v", err)
	}
	stored := f.store.strRows[appsettings.KeyMailPass]
	if stored == plaintext || strings.Contains(stored, plaintext) {
		t.Fatalf("settings row contains the plaintext: %q", stored)
	}
	if !strings.HasPrefix(stored, "sealed:") {
		t.Fatalf("settings row is not a cipher blob: %q", stored)
	}
	if got, err := f.cipher.OpenSecret("x", stored); err != nil || got != plaintext {
		t.Fatalf("cipher cannot open the stored blob: (%q, %v)", got, err)
	}
	entry := f.audits.entries[0]
	if strings.Contains(entry.Detail["changes"].(map[string][2]any)[appsettings.KeyMailPass][1].(string), plaintext) {
		t.Fatal("audit detail contains the plaintext")
	}
	passPair := entry.Detail["changes"].(map[string][2]any)[appsettings.KeyMailPass]
	if passPair[0] != "cleared" || passPair[1] != "set" {
		t.Fatalf("pass pair = %#v, want [cleared set]", passPair)
	}
	userPair := entry.Detail["changes"].(map[string][2]any)[appsettings.KeyMailUser]
	if userPair[0] != "cleared" || userPair[1] != "set" {
		t.Fatalf("user pair = %#v, want [cleared set]", userPair)
	}
	report, err := f.svc.MailSettings(context.Background(), corescope.Master())
	if err != nil {
		t.Fatalf("MailSettings: %v", err)
	}
	if !report.Pass.Set || report.Pass.Source != appsettings.MailSourceDB {
		t.Fatalf("pass report = %+v", report.Pass)
	}
}

// 域校验负例：一律 400 零写入零审计。
func TestSetMailRejectsInvalidChanges(t *testing.T) {
	cases := []struct {
		name   string
		change appsettings.MailChange
	}{
		{"unknown key", appsettings.MailChange{Key: "mail.unknown", Op: appsettings.OpSet, Value: "x"}},
		{"empty host", appsettings.MailChange{Key: appsettings.KeyMailSMTPHost, Op: appsettings.OpSet, Value: ""}},
		{"host with scheme", appsettings.MailChange{Key: appsettings.KeyMailSMTPHost, Op: appsettings.OpSet, Value: "smtp://x"}},
		{"port zero", appsettings.MailChange{Key: appsettings.KeyMailSMTPPort, Op: appsettings.OpSet, Value: 0}},
		{"port too high", appsettings.MailChange{Key: appsettings.KeyMailSMTPPort, Op: appsettings.OpSet, Value: 65536}},
		{"port negative", appsettings.MailChange{Key: appsettings.KeyMailSMTPPort, Op: appsettings.OpSet, Value: -1}},
		{"port wrong type", appsettings.MailChange{Key: appsettings.KeyMailSMTPPort, Op: appsettings.OpSet, Value: "587"}},
		{"tls unknown", appsettings.MailChange{Key: appsettings.KeyMailTLSMode, Op: appsettings.OpSet, Value: "ssl"}},
		{"tls uppercase", appsettings.MailChange{Key: appsettings.KeyMailTLSMode, Op: appsettings.OpSet, Value: "AUTO"}},
		{"from empty", appsettings.MailChange{Key: appsettings.KeyMailFrom, Op: appsettings.OpSet, Value: ""}},
		{"from invalid", appsettings.MailChange{Key: appsettings.KeyMailFrom, Op: appsettings.OpSet, Value: "not-an-address"}},
		{"user empty", appsettings.MailChange{Key: appsettings.KeyMailUser, Op: appsettings.OpSet, Value: "  "}},
		{"pass empty", appsettings.MailChange{Key: appsettings.KeyMailPass, Op: appsettings.OpSet, Value: ""}},
		{"clear with value", appsettings.MailChange{Key: appsettings.KeyMailSMTPHost, Op: appsettings.OpClear, Value: "x"}},
		{"unknown op", appsettings.MailChange{Key: appsettings.KeyMailSMTPHost, Op: "merge", Value: "x"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newMailFixture(t, mailEnv(), newMailCipher())
			err := f.svc.SetMail(context.Background(), corescope.Master(), scheduleActor,
				[]appsettings.MailChange{tc.change})
			if err == nil || errs.From(err).Code != errs.CodeValidation {
				t.Fatalf("err = %v, want validation_failed", err)
			}
			if len(f.store.strWrites) != 0 || len(f.store.clears) != 0 || len(f.audits.entries) != 0 {
				t.Fatalf("被拒写有副作用: writes=%v clears=%v audit=%d",
					f.store.strWrites, f.store.clears, len(f.audits.entries))
			}
		})
	}
	// 空更新单独一条：入口层也会挡，用例层不得静默成功。
	f := newMailFixture(t, mailEnv(), newMailCipher())
	if err := f.svc.SetMail(context.Background(), corescope.Master(), scheduleActor, nil); err == nil ||
		errs.From(err).Code != errs.CodeValidation {
		t.Fatalf("empty update err = %v, want validation_failed", err)
	}
}

// 加密失败与回验失败都 500 零写入零审计。
func TestSetMailEncryptionFailureIsInternalNoWrite(t *testing.T) {
	for _, tc := range []struct {
		name   string
		cipher *mailCipher
	}{
		{"seal fails", &mailCipher{sealed: map[string]string{}, failSeal: true}},
		{"open fails", &mailCipher{sealed: map[string]string{}, failOpen: true}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := newMailFixture(t, mailEnv(), tc.cipher)
			err := f.svc.SetMail(context.Background(), corescope.Master(), scheduleActor,
				[]appsettings.MailChange{
					{Key: appsettings.KeyMailPass, Op: appsettings.OpSet, Value: "secret"},
				})
			if err == nil || errs.From(err).Code != errs.CodeInternal {
				t.Fatalf("err = %v, want internal", err)
			}
			if len(f.store.strWrites) != 0 || len(f.store.clears) != 0 || len(f.audits.entries) != 0 {
				t.Fatalf("加密失败仍有副作用: writes=%v clears=%v audit=%d",
					f.store.strWrites, f.store.clears, len(f.audits.entries))
			}
		})
	}
}

// 审计失败整体回滚：错误上抛，事务标记回滚。
func TestSetMailAuditFailureRollsBack(t *testing.T) {
	f := newMailFixture(t, mailEnv(), newMailCipher())
	f.audits.err = errors.New("audit down")
	err := f.svc.SetMail(context.Background(), corescope.Master(), scheduleActor,
		[]appsettings.MailChange{
			{Key: appsettings.KeyMailSMTPHost, Op: appsettings.OpSet, Value: "smtp.new.example"},
		})
	if err == nil {
		t.Fatal("SetMail succeeded with a failing audit")
	}
	if !f.tx.rolledBack {
		t.Fatal("transaction was not marked rolled back")
	}
}

// 每发送解析：Resolve 返回生效配置；DB 行改值后下一次解析即新值。
func TestMailResolverResolvesPerCall(t *testing.T) {
	cipher := newMailCipher()
	f := newMailFixture(t, mailEnv(), cipher)
	_, cfg, err := appsettings.NewMailResolver(f.store, cipher, mailEnv(), nil).Resolve(context.Background())
	if err != nil {
		t.Fatalf("Resolve: %v", err)
	}
	if cfg.Host != "smtp.env.example" || cfg.Port != 465 || cfg.Pass != "env-pass" {
		t.Fatalf("initial config = %+v", cfg)
	}
	if err := f.svc.SetMail(context.Background(), corescope.Master(), scheduleActor,
		[]appsettings.MailChange{
			{Key: appsettings.KeyMailSMTPHost, Op: appsettings.OpSet, Value: "smtp.next.example"},
			{Key: appsettings.KeyMailSMTPPort, Op: appsettings.OpSet, Value: 2525},
		}); err != nil {
		t.Fatalf("SetMail: %v", err)
	}
	_, cfg, err = appsettings.NewMailResolver(f.store, cipher, mailEnv(), nil).Resolve(context.Background())
	if err != nil {
		t.Fatalf("Resolve after write: %v", err)
	}
	if cfg.Host != "smtp.next.example" || cfg.Port != 2525 {
		t.Fatalf("config after write = %+v", cfg)
	}
}

// IPv6 字面量是合法 SMTP 主机：写面与审计形状都必须放行。
func TestSetMailAcceptsIPv6Host(t *testing.T) {
	f := newMailFixture(t, mailEnv(), newMailCipher())
	if err := f.svc.SetMail(context.Background(), corescope.Master(), scheduleActor,
		[]appsettings.MailChange{
			{Key: appsettings.KeyMailSMTPHost, Op: appsettings.OpSet, Value: "::1"},
		}); err != nil {
		t.Fatalf("SetMail(::1): %v", err)
	}
	if f.store.strRows[appsettings.KeyMailSMTPHost] != "::1" {
		t.Fatalf("host row = %q", f.store.strRows[appsettings.KeyMailSMTPHost])
	}
}
