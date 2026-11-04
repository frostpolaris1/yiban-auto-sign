package updatecheck

// 自检逐项判据。
//
// # 覆盖
// 配置、数据库、工具、目录、密钥、服务六面各有用例；
// 每条 FAIL 必须带提示、每条 SKIP 必须写原因；
// 输出（文本与 JSON）不得含 DSN 口令或密钥原文。

import (
	"context"
	"encoding/base64"
	"errors"
	"os"
	"strings"
	"testing"
)

// wantCheckNames 是检查清单的唯一顺序；U2 依赖它保持稳定。
var wantCheckNames = []string{
	"config.dsn",
	"config.http_addr",
	"config.ui_dir",
	"config.photo_dir",
	"db.connect",
	"db.schema_version",
	"db.migrations_embedded",
	"db.migrations_pending",
	"tools.pg_dump",
	"tools.pg_restore",
	"tools.psql",
	"tools.gpg",
	"tools.tar",
	"fs.photo_dir",
	"fs.backup_dir",
	"keys.credential_key",
	"keys.hash_pepper",
	"keys.state",
	"service.yiban",
}

func TestRunAllPassWithStateSkipped(t *testing.T) {
	f := newFixture()
	rep := f.run(t)
	if got := checkNames(rep); strings.Join(got, ",") != strings.Join(wantCheckNames, ",") {
		t.Fatalf("check order = %v, want %v", got, wantCheckNames)
	}
	if rep.ExitCode() != 0 {
		t.Fatalf("exit = %d, want 0; text:\n%s", rep.ExitCode(), rep.Text())
	}
	state := byName(t, rep, nameKeysState)
	if state.Status != StatusSkip || !strings.Contains(state.Detail, "状态文件不存在") {
		t.Fatalf("keys.state = %+v, want SKIP with reason", state)
	}
	if rep.Passed() != len(wantCheckNames)-1 || rep.Failed() != 0 || rep.Skipped() != 1 {
		t.Fatalf("counts = %d/%d/%d", rep.Passed(), rep.Failed(), rep.Skipped())
	}
	if !f.db.closed {
		t.Fatal("database handle was not closed")
	}
}

func TestMissingDSNFailsConfigAndSkipsDatabase(t *testing.T) {
	f := newFixture()
	f.opts.DSN = ""
	rep := f.run(t)
	if byName(t, rep, nameConfigDSN).Status != StatusFail {
		t.Fatalf("%s = %+v, want FAIL", nameConfigDSN, byName(t, rep, nameConfigDSN))
	}
	for _, name := range []string{nameDBConnect, nameDBSchemaVersion, nameDBPending} {
		if byName(t, rep, name).Status != StatusSkip {
			t.Fatalf("%s = %+v, want SKIP", name, byName(t, rep, name))
		}
	}
	if rep.ExitCode() != 1 {
		t.Fatalf("exit = %d, want 1", rep.ExitCode())
	}
}

func TestDSNShapeFailureDoesNotEchoInput(t *testing.T) {
	f := newFixture()
	f.opts.DSN = "not-a-dsn-with-secret"
	rep := f.run(t)
	c := byName(t, rep, nameConfigDSN)
	if c.Status != StatusFail {
		t.Fatalf("%s = %+v", nameConfigDSN, c)
	}
	if strings.Contains(c.Detail, f.opts.DSN) {
		t.Fatalf("detail echoes DSN: %q", c.Detail)
	}
}

func TestDSNTargetForms(t *testing.T) {
	cases := []struct {
		name string
		dsn  string
		want string
	}{
		{"uri", testDSN, "127.0.0.1:5432/yiban"},
		{"uri-encoded-password", "postgresql://u:p%40ss@db.internal:5432/app?sslmode=require", "db.internal:5432/app"},
		{"keyword", "host=db.internal port=5432 dbname=yiban password='s3 cret' user=yiban", "db.internal:5432/yiban"},
		{"keyword-hostaddr", "hostaddr=10.0.0.5 dbname=yiban", "10.0.0.5/yiban"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := dsnTarget(tc.dsn)
			if err != nil {
				t.Fatalf("dsnTarget: %v", err)
			}
			if got != tc.want {
				t.Fatalf("dsnTarget() = %q, want %q", got, tc.want)
			}
			for _, secret := range []string{"s3cret", "s3 cret", "yiban:s3", "p@ss"} {
				if strings.Contains(got, secret) {
					t.Fatalf("target leaks %q: %q", secret, got)
				}
			}
		})
	}
}

func TestDBConnectErrorRedactsPassword(t *testing.T) {
	f := newFixture()
	f.deps.OpenDB = func(context.Context, string) (DB, error) {
		return nil, errors.New("failed to connect: " + testDSN + " password=" + testDSNPassword)
	}
	rep := f.run(t)
	c := byName(t, rep, nameDBConnect)
	if c.Status != StatusFail {
		t.Fatalf("%s = %+v", nameDBConnect, c)
	}
	assertNoSecrets(t, rep.Text())
	body, err := rep.JSON()
	if err != nil {
		t.Fatalf("JSON: %v", err)
	}
	assertNoSecrets(t, string(body))
}

func TestDBPendingAndAhead(t *testing.T) {
	t.Run("behind-binary", func(t *testing.T) {
		f := newFixture()
		f.db.version = 21
		f.embedded = 22
		rep := f.run(t)
		c := byName(t, rep, nameDBPending)
		if c.Status != StatusFail || !strings.Contains(c.Detail, "有待执行迁移") {
			t.Fatalf("%s = %+v", nameDBPending, c)
		}
		if byName(t, rep, nameDBSchemaVersion).Status != StatusPass {
			t.Fatalf("schema version row should still pass")
		}
	})
	t.Run("ahead", func(t *testing.T) {
		f := newFixture()
		f.db.version = 24
		f.embedded = 23
		rep := f.run(t)
		c := byName(t, rep, nameDBPending)
		if c.Status != StatusFail || !strings.Contains(c.Detail, "数据库超前") {
			t.Fatalf("%s = %+v", nameDBPending, c)
		}
	})
	t.Run("unmigrated", func(t *testing.T) {
		f := newFixture()
		f.db.version = 0
		rep := f.run(t)
		if c := byName(t, rep, nameDBSchemaVersion); c.Status != StatusPass || !strings.Contains(c.Detail, "尚未迁移") {
			t.Fatalf("schema = %+v", c)
		}
		if byName(t, rep, nameDBPending).Status != StatusFail {
			t.Fatalf("unmigrated database must fail pending check")
		}
	})
}

func TestDBSchemaReadErrorSkipsPending(t *testing.T) {
	f := newFixture()
	f.db.err = errors.New("permission denied for table goose_db_version")
	rep := f.run(t)
	if c := byName(t, rep, nameDBSchemaVersion); c.Status != StatusFail {
		t.Fatalf("schema = %+v", c)
	}
	if c := byName(t, rep, nameDBPending); c.Status != StatusSkip {
		t.Fatalf("pending = %+v, want SKIP", c)
	}
}

func TestEmbeddedVersionFailures(t *testing.T) {
	t.Run("missing-probe", func(t *testing.T) {
		f := newFixture()
		f.deps.EmbeddedVersion = nil
		rep := f.run(t)
		if c := byName(t, rep, nameDBEmbedded); c.Status != StatusSkip {
			t.Fatalf("embedded = %+v", c)
		}
		if c := byName(t, rep, nameDBPending); c.Status != StatusSkip {
			t.Fatalf("pending = %+v", c)
		}
	})
	t.Run("error", func(t *testing.T) {
		f := newFixture()
		f.deps.EmbeddedVersion = func() (int64, error) { return 0, errors.New("no migrations") }
		rep := f.run(t)
		if c := byName(t, rep, nameDBEmbedded); c.Status != StatusFail {
			t.Fatalf("embedded = %+v", c)
		}
	})
	t.Run("zero", func(t *testing.T) {
		f := newFixture()
		f.deps.EmbeddedVersion = func() (int64, error) { return 0, nil }
		rep := f.run(t)
		if c := byName(t, rep, nameDBEmbedded); c.Status != StatusFail {
			t.Fatalf("embedded = %+v", c)
		}
	})
}

func TestNoDBProbeSkipsDatabase(t *testing.T) {
	f := newFixture()
	f.deps.OpenDB = nil
	rep := f.run(t)
	if c := byName(t, rep, nameDBConnect); c.Status != StatusSkip {
		t.Fatalf("connect = %+v", c)
	}
}

func TestHTTPAddrValidation(t *testing.T) {
	cases := []struct {
		addr string
		want Status
	}{
		{"", StatusFail},
		{"127.0.0.1", StatusFail},
		{"127.0.0.1:0", StatusFail},
		{"127.0.0.1:70000", StatusFail},
		{"127.0.0.1:47183", StatusPass},
		{":8080", StatusPass},
	}
	for _, tc := range cases {
		t.Run(tc.addr, func(t *testing.T) {
			f := newFixture()
			f.opts.HTTPAddr = tc.addr
			if got := byName(t, f.run(t), nameConfigHTTPAddr).Status; got != tc.want {
				t.Fatalf("status = %s, want %s", got, tc.want)
			}
		})
	}
}

func TestUIDirChecks(t *testing.T) {
	cases := []struct {
		name  string
		setup func(*fixture)
		want  Status
	}{
		{"unset", func(f *fixture) { f.opts.UIDir = "" }, StatusSkip},
		{"missing", func(f *fixture) { f.opts.UIDir = "/srv/missing" }, StatusFail},
		{"no-index", func(f *fixture) { f.fs.dirs["/srv/ui"] = []os.DirEntry{fakeEntry{"assets"}} }, StatusFail},
		{"stat-error", func(f *fixture) { f.fs.statErr["/srv/ui"] = errors.New("permission denied") }, StatusFail},
		{"unreadable", func(f *fixture) { f.fs.readErr["/srv/ui"] = errors.New("permission denied") }, StatusFail},
		{"ok", func(*fixture) {}, StatusPass},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newFixture()
			tc.setup(f)
			if got := byName(t, f.run(t), nameConfigUIDir).Status; got != tc.want {
				t.Fatalf("status = %s, want %s", got, tc.want)
			}
		})
	}
}

func TestPhotoDirChecks(t *testing.T) {
	t.Run("missing", func(t *testing.T) {
		f := newFixture()
		delete(f.fs.dirs, "/var/lib/yiban/photos")
		rep := f.run(t)
		if c := byName(t, rep, nameConfigPhotoDir); c.Status != StatusSkip {
			t.Fatalf("config = %+v", c)
		}
		if c := byName(t, rep, nameFSPhotoDir); c.Status != StatusSkip {
			t.Fatalf("fs = %+v", c)
		}
	})
	t.Run("not-writable", func(t *testing.T) {
		f := newFixture()
		f.fs.probeErr["/var/lib/yiban/photos"] = errors.New("permission denied")
		rep := f.run(t)
		if c := byName(t, rep, nameConfigPhotoDir); c.Status != StatusPass {
			t.Fatalf("config = %+v", c)
		}
		if c := byName(t, rep, nameFSPhotoDir); c.Status != StatusFail {
			t.Fatalf("fs = %+v", c)
		}
	})
	t.Run("not-a-dir", func(t *testing.T) {
		f := newFixture()
		f.fs.files["/var/lib/yiban/photos"] = []byte("x")
		delete(f.fs.dirs, "/var/lib/yiban/photos")
		rep := f.run(t)
		if byName(t, rep, nameConfigPhotoDir).Status != StatusFail {
			t.Fatalf("config should fail")
		}
		if byName(t, rep, nameFSPhotoDir).Status != StatusFail {
			t.Fatalf("fs should fail")
		}
	})
	t.Run("unreadable", func(t *testing.T) {
		f := newFixture()
		f.fs.readErr["/var/lib/yiban/photos"] = errors.New("permission denied")
		if got := byName(t, f.run(t), nameConfigPhotoDir).Status; got != StatusFail {
			t.Fatalf("config = %s, want FAIL", got)
		}
	})
	t.Run("empty", func(t *testing.T) {
		f := newFixture()
		f.opts.PhotoDir = ""
		rep := f.run(t)
		if byName(t, rep, nameConfigPhotoDir).Status != StatusFail {
			t.Fatalf("config should fail")
		}
		if byName(t, rep, nameFSPhotoDir).Status != StatusFail {
			t.Fatalf("fs should fail")
		}
	})
}

func TestBackupDirChecks(t *testing.T) {
	cases := []struct {
		name  string
		setup func(*fixture)
		want  Status
	}{
		{"writable", func(*fixture) {}, StatusPass},
		{"not-writable", func(f *fixture) {
			f.fs.probeErr["/var/lib/yiban/pre-migrate"] = errors.New("permission denied")
		}, StatusFail},
		{"is-file", func(f *fixture) {
			f.fs.files["/var/lib/yiban/pre-migrate"] = []byte("x")
			delete(f.fs.dirs, "/var/lib/yiban/pre-migrate")
		}, StatusFail},
		{"missing-parent-writable", func(f *fixture) {
			delete(f.fs.dirs, "/var/lib/yiban/pre-migrate")
		}, StatusSkip},
		{"missing-parent-unwritable", func(f *fixture) {
			delete(f.fs.dirs, "/var/lib/yiban/pre-migrate")
			f.fs.probeErr["/var/lib/yiban"] = errors.New("read-only file system")
		}, StatusFail},
		{"missing-no-ancestor", func(f *fixture) {
			f.opts.BackupDir = "/nope/pre-migrate"
		}, StatusFail},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newFixture()
			tc.setup(f)
			c := byName(t, f.run(t), nameFSBackupDir)
			if c.Status != tc.want {
				t.Fatalf("status = %s, want %s (%+v)", c.Status, tc.want, c)
			}
		})
	}
}

func TestToolsChecks(t *testing.T) {
	t.Run("missing", func(t *testing.T) {
		f := newFixture()
		f.deps.LookPath = func(name string) (string, error) {
			if name == "pg_restore" {
				return "", errors.New("not found")
			}
			return "/usr/bin/" + name, nil
		}
		rep := f.run(t)
		if c := byName(t, rep, "tools.pg_restore"); c.Status != StatusFail || c.Hint == "" {
			t.Fatalf("pg_restore = %+v", c)
		}
		if byName(t, rep, "tools.pg_dump").Status != StatusPass {
			t.Fatalf("pg_dump should pass")
		}
	})
	t.Run("not-injected", func(t *testing.T) {
		f := newFixture()
		f.deps.LookPath = nil
		rep := f.run(t)
		for _, name := range []string{"tools.pg_dump", "tools.psql", "tools.tar"} {
			if c := byName(t, rep, name); c.Status != StatusSkip || c.Detail == "" {
				t.Fatalf("%s = %+v", name, c)
			}
		}
	})
}

func TestKeyFingerprintChecks(t *testing.T) {
	cases := []struct {
		name   string
		modify func(*fixture)
		target string
		want   Status
	}{
		{"credential-missing", func(f *fixture) { f.opts.CredentialKey = "" }, nameKeysCredential, StatusFail},
		{"credential-bad-base64", func(f *fixture) { f.opts.CredentialKey = "not base64" }, nameKeysCredential, StatusFail},
		{"credential-short", func(f *fixture) {
			f.opts.CredentialKey = base64.StdEncoding.EncodeToString([]byte("short"))
		}, nameKeysCredential, StatusFail},
		{"pepper-missing", func(f *fixture) { f.opts.HashPepper = "" }, nameKeysPepper, StatusFail},
		{"pepper-short", func(f *fixture) { f.opts.HashPepper = "short" }, nameKeysPepper, StatusFail},
		{"ok", func(*fixture) {}, nameKeysCredential, StatusPass},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newFixture()
			tc.modify(f)
			rep := f.run(t)
			c := byName(t, rep, tc.target)
			if c.Status != tc.want {
				t.Fatalf("%s = %+v, want %s", tc.target, c, tc.want)
			}
			assertNoSecrets(t, rep.Text())
		})
	}
}

func TestFingerprintsAreDomainSeparatedAndStable(t *testing.T) {
	a := fingerprint(fingerprintDomainCredentialKey, []byte("same"))
	b := fingerprint(fingerprintDomainHashPepper, []byte("same"))
	if a == b {
		t.Fatal("fingerprints for different domains must differ")
	}
	if a != fingerprint(fingerprintDomainCredentialKey, []byte("same")) {
		t.Fatal("fingerprint is not stable")
	}
	decoded := fingerprint(fingerprintDomainCredentialKey, []byte("0123456789abcdef0123456789abcdef"))
	got, err := CredentialKeyFingerprint(testCredentialKey)
	if err != nil {
		t.Fatalf("CredentialKeyFingerprint: %v", err)
	}
	if got != decoded {
		t.Fatalf("credential fingerprint must hash the decoded key, got %s", ShortFingerprint(got))
	}
}

func TestShortFingerprint(t *testing.T) {
	full := "sha256:" + strings.Repeat("a", 64)
	if got := ShortFingerprint(full); got == full || !strings.HasSuffix(got, "…") {
		t.Fatalf("ShortFingerprint() = %q", got)
	}
	if got := ShortFingerprint("raw"); got != "raw" {
		t.Fatalf("non-fingerprint should pass through, got %q", got)
	}
}

func TestKeyStateComparison(t *testing.T) {
	t.Run("match", func(t *testing.T) {
		f := newFixture()
		if _, err := RecordKeys(f.fs, f.opts.StatePath, f.opts, fixedNow); err != nil {
			t.Fatalf("RecordKeys: %v", err)
		}
		c := byName(t, f.run(t), nameKeysState)
		if c.Status != StatusPass || !strings.Contains(c.Detail, fixedNow.Format("2006-01-02T15:04:05Z")) {
			t.Fatalf("keys.state = %+v", c)
		}
	})
	t.Run("mismatch", func(t *testing.T) {
		f := newFixture()
		other := f.opts
		other.CredentialKey = base64.StdEncoding.EncodeToString([]byte("another-32-byte-credential-key!!"))
		if _, err := RecordKeys(f.fs, f.opts.StatePath, other, fixedNow); err != nil {
			t.Fatalf("RecordKeys: %v", err)
		}
		rep := f.run(t)
		c := byName(t, rep, nameKeysState)
		if c.Status != StatusFail || !strings.Contains(c.Detail, "credential_key 指纹变化") {
			t.Fatalf("keys.state = %+v", c)
		}
		assertNoSecrets(t, rep.Text())
	})
	t.Run("missing-name", func(t *testing.T) {
		f := newFixture()
		f.fs.files[f.opts.StatePath] = []byte(`{"schema_version":1,"key_fingerprints":{"credential_key":"sha256:aa"},"recorded_at":"2026-01-01T00:00:00Z"}`)
		c := byName(t, f.run(t), nameKeysState)
		if c.Status != StatusFail || !strings.Contains(c.Detail, "hash_pepper 在记录里缺失") {
			t.Fatalf("keys.state = %+v", c)
		}
	})
	t.Run("corrupt", func(t *testing.T) {
		f := newFixture()
		f.fs.files[f.opts.StatePath] = []byte("{")
		c := byName(t, f.run(t), nameKeysState)
		if c.Status != StatusFail {
			t.Fatalf("keys.state = %+v", c)
		}
	})
	t.Run("future-schema", func(t *testing.T) {
		f := newFixture()
		f.fs.files[f.opts.StatePath] = []byte(`{"schema_version":2,"key_fingerprints":{},"recorded_at":""}`)
		c := byName(t, f.run(t), nameKeysState)
		if c.Status != StatusFail || !strings.Contains(c.Detail, "拒绝降级") {
			t.Fatalf("keys.state = %+v", c)
		}
	})
	t.Run("keys-broken", func(t *testing.T) {
		f := newFixture()
		f.opts.CredentialKey = ""
		f.fs.files[f.opts.StatePath] = []byte(`{"schema_version":1,"key_fingerprints":{},"recorded_at":""}`)
		c := byName(t, f.run(t), nameKeysState)
		if c.Status != StatusSkip || !strings.Contains(c.Detail, "前置密钥检查未通过") {
			t.Fatalf("keys.state = %+v", c)
		}
	})
}

func TestServiceChecks(t *testing.T) {
	cases := []struct {
		name  string
		probe ServiceProbe
		want  Status
	}{
		{"not-injected", nil, StatusSkip},
		{"no-systemd", func(context.Context) (bool, bool, string) { return false, false, "" }, StatusSkip},
		{"inactive", func(context.Context) (bool, bool, string) { return true, false, "inactive" }, StatusFail},
		{"active", func(context.Context) (bool, bool, string) { return true, true, "active" }, StatusPass},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newFixture()
			f.deps.ServiceProbe = tc.probe
			c := byName(t, f.run(t), nameService)
			if c.Status != tc.want {
				t.Fatalf("%s = %+v, want %s", nameService, c, tc.want)
			}
		})
	}
}

func TestEveryFailureHasHintAndEverySkipHasReason(t *testing.T) {
	f := newFixture()
	f.opts.DSN = ""
	f.opts.UIDir = ""
	f.opts.PhotoDir = "/missing/photos"
	f.deps.LookPath = nil
	f.deps.ServiceProbe = nil
	f.deps.OpenDB = nil
	f.deps.EmbeddedVersion = nil
	rep := f.run(t)
	for _, c := range rep.Checks {
		switch c.Status {
		case StatusFail:
			if strings.TrimSpace(c.Hint) == "" {
				t.Fatalf("%s FAIL without hint", c.Name)
			}
		case StatusSkip:
			if strings.TrimSpace(c.Detail) == "" {
				t.Fatalf("%s SKIP without reason", c.Name)
			}
		}
	}
}

func TestReportExitCode(t *testing.T) {
	rep := Report{Checks: []Check{
		{Name: "a", Status: StatusPass},
		{Name: "b", Status: StatusSkip, Detail: "reason"},
	}}
	if rep.ExitCode() != 0 {
		t.Fatalf("PASS+SKIP exit = %d, want 0", rep.ExitCode())
	}
	rep.Checks = append(rep.Checks, Check{Name: "c", Status: StatusFail, Hint: "fix"})
	if rep.ExitCode() != 1 {
		t.Fatalf("FAIL exit = %d, want 1", rep.ExitCode())
	}
}

func TestSanitizeText(t *testing.T) {
	cases := []struct {
		name string
		text string
		dsn  string
	}{
		{"uri-whole", "dial " + testDSN + " failed", testDSN},
		{"uri-password-only", "password " + testDSNPassword + " rejected", testDSN},
		{"keyword", "connection failed: password='s3 cret' host=db", "host=db password='s3 cret'"},
		{"generic-password-field", "error: password=hunter2 rejected", ""},
		{"generic-userinfo", "error: postgres://alice:hunter2@db:5432/x failed", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := sanitizeText(tc.text, tc.dsn)
			for _, secret := range []string{"s3cret-pass-word", "s3 cret", "hunter2"} {
				if strings.Contains(got, secret) {
					t.Fatalf("sanitized text still contains %q: %q", secret, got)
				}
			}
		})
	}
}

// assertNoSecrets 断言文本里没有测试口令与密钥原文。
func assertNoSecrets(t *testing.T, text string) {
	t.Helper()
	for _, secret := range []string{
		testDSNPassword,
		testCredentialKey,
		testPepper,
		"0123456789abcdef0123456789abcdef",
	} {
		if strings.Contains(text, secret) {
			t.Fatalf("output leaks secret %q:\n%s", secret, text)
		}
	}
}
