package main

// doctor 的表驱动测试。
//
// # 功能
// 每条检查给健康与故障两态：注入故障 → 该条 FAIL 且含修复命令；
// 前置不满足 → SKIP 且写明原因。全部外供依赖经 probe 注入，
// 本文件**不**连真实数据库、**不**占真实端口、**不**依赖平台特性。
//
// # 口径
// 判据来自派单 P7：「注入故障环境 → doctor 报该条 → 输出含修复命令」。
// Windows 上无法构造的故障（如去掉 POSIX 执行位）显式 t.Skip 并打印原因。

import (
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
)

// testKeyB64 是合法 32 字节钥的 base64（"0123456789abcdef" 重复两遍）。
const testKeyB64 = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="

const testPepper = "0123456789abcdef0123456789abcdef"

// fakeSource 是可枚举的环境替身。
type fakeSource map[string]string

func (f fakeSource) Lookup(key string) (string, bool) {
	v, ok := f[key]
	return v, ok
}

func (f fakeSource) Environ() []string {
	out := make([]string, 0, len(f))
	for k, v := range f {
		out = append(out, k+"="+v)
	}
	return out
}

// fakeDB 是 dbHandle 替身：按语义方法返回固定值。
type fakeDB struct {
	version   int64
	serverNum int64
	tables    map[string]bool
	rows      map[string]int64
	now       time.Time
	verifyErr error
	closed    bool
}

func (f *fakeDB) Close() { f.closed = true }

func (f *fakeDB) SchemaVersion(context.Context) (int64, error) { return f.version, nil }

func (f *fakeDB) ServerVersionNum(context.Context) (int64, error) { return f.serverNum, nil }

func (f *fakeDB) TableExists(_ context.Context, name string) (bool, error) {
	return f.tables[name], nil
}

func (f *fakeDB) DBNow(context.Context) (time.Time, error) { return f.now, nil }

func (f *fakeDB) Count(_ context.Context, table, _ string, _ ...any) (int64, error) {
	return f.rows[table], nil
}

func (f *fakeDB) AuditVerify(context.Context) (audit.Report, error) {
	if f.verifyErr != nil {
		return audit.Report{}, f.verifyErr
	}
	return audit.Report{Count: 3}, nil
}

// healthyProbe 返回全绿探针与其数据库替身。
func healthyProbe(t *testing.T) (*probe, *fakeDB) {
	t.Helper()
	dir := t.TempDir()
	binName := "yiban-server"
	if runtime.GOOS == "windows" {
		// Windows 的执行位按扩展名判定：无 .exe 的文件恒报不可执行。
		binName += ".exe"
	}
	bin := filepath.Join(t.TempDir(), binName)
	if err := os.WriteFile(bin, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatalf("write fake binary: %v", err)
	}
	reg := filepath.Join(t.TempDir(), "registry.json")
	regBody := `{"_meta":{},"YIBAN_ENV":{},"YIBAN_HASH_PEPPER":{},"YIBAN_CREDENTIAL_KEY":{},` +
		`"YIBAN_DATABASE_URL":{},"YIBAN_TZ":{},"YIBAN_HTTP_ADDR":{},"YIBAN_PHOTO_DIR":{}}`
	if err := os.WriteFile(reg, []byte(regBody), 0o644); err != nil {
		t.Fatalf("write registry: %v", err)
	}
	base := time.Date(2026, 10, 27, 12, 0, 0, 0, time.UTC)
	embedded, err := pg.EmbeddedVersion()
	if err != nil {
		t.Fatalf("embedded version: %v", err)
	}
	db := &fakeDB{
		version:   embedded, // 健康态 = 与二进制携带版本一致
		serverNum: 180008,
		tables:    expectedTableSet(),
		now:       base,
		rows:      map[string]int64{"egresses": 1, "school_tasks": 2},
	}
	p := &probe{
		src: fakeSource{
			"YIBAN_ENV":            "production",
			"YIBAN_HASH_PEPPER":    testPepper,
			"YIBAN_CREDENTIAL_KEY": testKeyB64,
			"YIBAN_DATABASE_URL":   "postgres://u:p@127.0.0.1:5432/db",
			"YIBAN_TZ":             "UTC",
			"YIBAN_HTTP_ADDR":      "127.0.0.1:0",
			"YIBAN_PHOTO_DIR":      dir,
		},
		registry: reg,
		binPath:  bin,
		// 健康态：当前进程就是服务用户，走实测写入路径。
		serviceUser: currentUserNameForTest(t),
		now:         func() time.Time { return base },
		tryListen: func(string) (closer, error) {
			return nopCloser{}, nil
		},
		httpGet: func(string, string) (int, error) { return 0, errors.New("no server") },
		openDB: func(context.Context, string, []byte) (dbHandle, error) {
			return db, nil
		},
		diskFree: func(string) (uint64, error) { return 8 << 30, nil },
		currentUser: func() (string, bool) {
			return currentUserNameForTest(t), true
		},
		dirStat: func(string) (dirInfo, bool) {
			me := currentUserNameForTest(t)
			return dirInfo{owner: me, group: me, perm: 0o755}, true
		},
		userGroups: func(name string) ([]string, bool) { return []string{name}, true },
	}
	return p, db
}

func currentUserNameForTest(t *testing.T) string {
	t.Helper()
	name, ok := currentUserName()
	if !ok {
		t.Skip("SKIP: 当前平台拿不到用户名，属主断言无法构造")
	}
	return name
}

// mustCurrentUser 取当前用户名；拿不到即跳过（用例无法构造）。
func mustCurrentUser(t *testing.T) string {
	t.Helper()
	return currentUserNameForTest(t)
}

func rowOf(t *testing.T, rep report, name string) checkRow {
	t.Helper()
	for _, r := range rep.rows {
		if r.name == name {
			return r
		}
	}
	t.Fatalf("check %s not found in report", name)
	return checkRow{}
}

func TestDoctorHealthyProbePasses(t *testing.T) {
	p, _ := healthyProbe(t)
	rep := runChecks(context.Background(), p)
	for _, r := range rep.rows {
		if r.fail {
			t.Errorf("%s: healthy probe must not FAIL: %s; fix=%q", r.name, r.detail, r.fix)
		}
	}
	if rep.failed > 0 {
		t.Fatalf("failed = %d, want 0; report:\n%s", rep.failed, rep.text())
	}
}

// checks 与故障注入用例表必须一一对应：新增检查不带用例不得通过
// （P7 审查 P3-7）。
func TestDoctorFaultCasesCoverEveryCheck(t *testing.T) {
	byName := map[string]bool{}
	for _, c := range faultCases(t) {
		byName[c.name] = true
	}
	for _, chk := range checks {
		if !byName[chk.name] {
			t.Fatalf("检查 %q 没有对应的故障注入用例", chk.name)
		}
	}
	if got := len(faultCases(t)); got != len(checks) {
		t.Fatalf("用例数 %d != 检查数 %d（多出的用例表项是死代码）", got, len(checks))
	}
}

type faultCase struct {
	name        string
	inject      func(p *probe, db *fakeDB)
	skipWindows string // 非 Windows 才能注入时写明原因
}

// faultCases 构造故障注入用例表。两个条目要用 t.TempDir()，
// 所以是函数而不是包级变量；覆盖断言与注入测试共用同一张表。
func faultCases(t *testing.T) []faultCase {
	return []faultCase{
		{"env.config", func(p *probe, _ *fakeDB) { p.src = fakeSource{"YIBAN_ENV": "prod"} }, ""},
		{"env.pepper", func(p *probe, _ *fakeDB) {
			p.src = fakeSource{"YIBAN_ENV": "production", "YIBAN_HASH_PEPPER": "short", "YIBAN_CREDENTIAL_KEY": testKeyB64}
		}, ""},
		{"env.credential_key", func(p *probe, _ *fakeDB) {
			p.src = fakeSource{"YIBAN_ENV": "production", "YIBAN_HASH_PEPPER": testPepper, "YIBAN_CREDENTIAL_KEY": "not-base64!!"}
		}, ""},
		{"env.tz_set", func(p *probe, _ *fakeDB) {
			p.src = fakeSource{"YIBAN_ENV": "production", "YIBAN_HASH_PEPPER": testPepper, "YIBAN_CREDENTIAL_KEY": testKeyB64}
		}, ""},
		{"env.tz_name", func(p *probe, _ *fakeDB) {
			p.src = fakeSource{"YIBAN_ENV": "production", "YIBAN_HASH_PEPPER": testPepper, "YIBAN_CREDENTIAL_KEY": testKeyB64, "YIBAN_TZ": "Mars/Olympus"}
		}, ""},
		{"env.unknown_keys", func(p *probe, _ *fakeDB) {
			// 夹具键在运行期拼出：字面量形状会被 config-registry 门禁
			// 当成生产键判红（.go 注释与字符串都计入扫描面）。
			p.src = fakeSource{"YIBAN_ENV": "production", "YIBAN_HASH_PEPPER": testPepper, "YIBAN_CREDENTIAL_KEY": testKeyB64, "YIBAN_TZ": "UTC", "YIBAN_" + "TYPO_KEY": "1"}
		}, ""},
		{"db.dsn", func(p *probe, _ *fakeDB) {
			p.src = fakeSource{"YIBAN_ENV": "production", "YIBAN_HASH_PEPPER": testPepper, "YIBAN_CREDENTIAL_KEY": testKeyB64}
			p.openDB = func(context.Context, string, []byte) (dbHandle, error) {
				return nil, errors.New("dial tcp 203.0.113.9:5432: connect refused")
			}
		}, ""},
		{"db.schema_version", func(_ *probe, db *fakeDB) { db.version = 15 }, ""},
		{"db.tables", func(_ *probe, db *fakeDB) { db.tables["photos"] = false }, ""},
		{"db.pg_version", func(_ *probe, db *fakeDB) { db.serverNum = 150006 }, ""},
		{"db.clock_offset", func(_ *probe, db *fakeDB) { db.now = db.now.Add(30 * time.Second) }, ""},
		{"db.audit_chain", func(_ *probe, db *fakeDB) { db.verifyErr = audit.ErrBrokenChain }, ""},
		{"db.pepper_rotation", func(_ *probe, db *fakeDB) { db.verifyErr = audit.ErrBrokenChain }, ""},
		{"db.egress", func(_ *probe, db *fakeDB) { delete(db.rows, "egresses") }, ""},
		{"db.school_tasks", func(_ *probe, db *fakeDB) { delete(db.rows, "school_tasks") }, ""},
		{"db.no_running", func(_ *probe, db *fakeDB) { db.rows["sign_tasks"] = 2 }, ""},
		{"fs.photo_dir", func(p *probe, _ *fakeDB) {
			blocker := filepath.Join(t.TempDir(), "blocker")
			if err := os.WriteFile(blocker, []byte("x"), 0o644); err != nil {
				t.Fatalf("write blocker: %v", err)
			}
			p.photoDir = filepath.Join(blocker, "sub") // 父路径是文件：创建必败
			// 与服务用户同一身份：本用例钉的是「实测写入失败」这一路。
			p.serviceUser = mustCurrentUser(t)
			p.currentUser = func() (string, bool) { return mustCurrentUser(t), true }
		}, ""},
		{"fs.disk", func(p *probe, _ *fakeDB) {
			p.diskFree = func(string) (uint64, error) { return 64 << 20, nil } // 64 MiB
		}, ""},
		{"fs.photo_owner", func(p *probe, _ *fakeDB) {
			// 属主不是服务用户。判据只看属主，与当前进程用户无关。
			p.dirStat = func(string) (dirInfo, bool) {
				return dirInfo{owner: "postgres", group: "postgres", perm: 0o700}, true
			}
		}, ""},
		{"net.http_addr", func(p *probe, _ *fakeDB) {
			p.tryListen = func(string) (closer, error) { return nil, errors.New("address already in use") }
			p.httpGet = func(string, string) (int, error) { return 0, errors.New("refused") }
		}, ""},
		{"bin.executable", func(p *probe, _ *fakeDB) {
			p.binPath = filepath.Join(t.TempDir(), "noexec")
			if err := os.WriteFile(p.binPath, []byte("x"), 0o444); err != nil {
				t.Fatalf("write noexec: %v", err)
			}
		}, "Windows FileMode always carries the exec bit"},
	}
}

func TestDoctorFaultInjectionHitsEachCheck(t *testing.T) {
	for _, tc := range faultCases(t) {
		t.Run(tc.name, func(t *testing.T) {
			if tc.skipWindows != "" && runtime.GOOS == "windows" {
				t.Skip("SKIP: " + tc.skipWindows)
			}
			p, db := healthyProbe(t)
			tc.inject(p, db)
			rep := runChecks(context.Background(), p)
			row := rowOf(t, rep, tc.name)
			if !row.fail {
				t.Fatalf("%s: want FAIL after injection, got detail=%q fix=%q", tc.name, row.detail, row.fix)
			}
			if strings.TrimSpace(row.fix) == "" {
				t.Fatalf("%s: FAIL must carry a fix command", tc.name)
			}
		})
	}
}

func TestDoctorSchemaVersionAheadFails(t *testing.T) {
	p, db := healthyProbe(t)
	db.version = 1000000 // 超前：禁止降级运行
	rep := runChecks(context.Background(), p)
	row := rowOf(t, rep, "db.schema_version")
	if !row.fail {
		t.Fatalf("schema ahead must FAIL, got %q", row.detail)
	}
}

func TestDoctorSkipCarriesReason(t *testing.T) {
	p, _ := healthyProbe(t)
	p.registry = filepath.Join(t.TempDir(), "missing.json") // 名册缺失
	p.diskFree = func(string) (uint64, error) { return 0, errors.New("statfs unsupported here") }
	rep := runChecks(context.Background(), p)
	for _, name := range []string{"env.unknown_keys", "fs.disk"} {
		row := rowOf(t, rep, name)
		if !row.skip {
			t.Fatalf("%s: want SKIP, got fail=%v detail=%q", name, row.fail, row.detail)
		}
		if strings.TrimSpace(row.detail+row.fix) == "" {
			t.Fatalf("%s: SKIP reason is empty", name)
		}
	}
}

func TestDoctorDBPreconditionSkipsDownstream(t *testing.T) {
	p, _ := healthyProbe(t)
	p.src = fakeSource{"YIBAN_ENV": "production", "YIBAN_HASH_PEPPER": testPepper, "YIBAN_CREDENTIAL_KEY": testKeyB64}
	p.openDB = func(context.Context, string, []byte) (dbHandle, error) {
		return nil, errors.New("dial tcp 203.0.113.9:5432: connect refused")
	}
	rep := runChecks(context.Background(), p)
	for _, r := range rep.rows {
		if strings.HasPrefix(r.name, "db.") && r.name != "db.dsn" {
			if !r.skip {
				t.Fatalf("%s: db.dsn failed, downstream must SKIP, got fail=%v", r.name, r.fail)
			}
			if !strings.Contains(r.detail+r.fix, "db.dsn") {
				t.Fatalf("%s: skip reason must name the failed precondition db.dsn", r.name)
			}
		}
	}
}

func TestDoctorFixTextCarriesRunnableCommand(t *testing.T) {
	// 派单判据「输出含修复命令」：抽查 pepper 的修复是一条可执行命令。
	p, _ := healthyProbe(t)
	p.src = fakeSource{"YIBAN_ENV": "production", "YIBAN_HASH_PEPPER": "short", "YIBAN_CREDENTIAL_KEY": testKeyB64}
	rep := runChecks(context.Background(), p)
	row := rowOf(t, rep, "env.pepper")
	if !strings.Contains(row.fix, "openssl rand") {
		t.Fatalf("env.pepper fix = %q, want an openssl command", row.fix)
	}
}

type nopCloser struct{}

func (nopCloser) Close() error { return nil }

// secretFragments 返回口令里所有 3 字符片段。
// 泄漏判据是「任何片段都不许出现」：只抹到第一个空格时，
// 尾部片段仍在输出里，单看整串会漏掉。
func secretFragments(secret string) []string {
	var out []string
	for _, word := range strings.Fields(secret) {
		for i := 0; i+3 <= len(word); i++ {
			out = append(out, word[i:i+3])
		}
	}
	return out
}

// TestRedactDSNRewritesEveryShape 钉住 DSN 打印时口令不泄漏（派单 F6、P1-2）。
// 六种形状：URI、URI 查询串、无 scheme 的 user:pass@host、keyword 无空格、
// = 两侧带空格、单引号含空白。输出不得含口令的任何片段。
func TestRedactDSNRewritesEveryShape(t *testing.T) {
	cases := []struct{ name, dsn, secret, keep string }{
		{"uri", "postgres://yiban:s3cr3tTail@db:5432/yiban", "s3cr3tTail", "db:5432/yiban"},
		{"uri-query", "postgres://db:5432/yiban?password=s3cr3tTail", "s3cr3tTail", "db:5432/yiban"},
		{"keyword", "host=db port=5432 user=yiban password=s3cr3tTail dbname=yiban", "s3cr3tTail", "dbname=yiban"},
		{"keyword-spaced", "host=db password = s3cr3tTail dbname=yiban", "s3cr3tTail", "dbname=yiban"},
		{"keyword-quoted", "host=db password='s3c r3t tail' dbname=yiban", "s3c r3t tail", "dbname=yiban"},
		{"bare", "yiban:s3cr3tTail@db:5432/yiban", "s3cr3tTail", "db:5432/yiban"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := redactDSN(tc.dsn)
			if strings.Contains(got, tc.secret) {
				t.Fatalf("redactDSN(%q) = %q：口令整串泄漏到输出", tc.dsn, got)
			}
			for _, frag := range secretFragments(tc.secret) {
				if strings.Contains(got, frag) {
					t.Fatalf("redactDSN(%q) = %q：口令片段 %q 残留（只抹到第一个空格）", tc.dsn, got, frag)
				}
			}
			if !strings.Contains(got, tc.keep) {
				t.Fatalf("redactDSN(%q) = %q：主机与库名必须保留，否则定位不了故障", tc.dsn, got)
			}
		})
	}
}

// TestRedactDSNKeepsSecretlessDSN 无口令的 DSN 原样返回。
func TestRedactDSNKeepsSecretlessDSN(t *testing.T) {
	for _, dsn := range []string{
		"postgres://db:5432/yiban",
		"host=db port=5432 dbname=yiban",
	} {
		if got := redactDSN(dsn); got != dsn {
			t.Fatalf("redactDSN(%q) = %q，want unchanged", dsn, got)
		}
	}
}

// 解析不了的 keyword DSN 必须失败关闭：不能因为词法放弃就把原文打出去。
func TestRedactDSNFailsClosedOnMalformedKeyword(t *testing.T) {
	for _, dsn := range []string{
		"host=db user=u password=s3cr3tTail dbname=yiban --prod",
		"host=db user=u password='s3c r3t' dbname=yiban #comment",
	} {
		got := redactDSN(dsn)
		if strings.Contains(got, "s3cr3t") || strings.Contains(got, "s3c") {
			t.Fatalf("redactDSN(%q) = %q：解析失败时口令泄漏", dsn, got)
		}
		if got != "<无法脱敏的 DSN>" {
			t.Fatalf("redactDSN(%q) = %q，want placeholder", dsn, got)
		}
	}
}

// TestDoctorOwnerChecksUseServiceUser 钉住属主与可写性按服务用户判（派单 F7）。
// root 跑 doctor、属主是 yiban 是生产常态：不能因为 root 自己可写就报问题，
// 也不能把自己的可写性当成服务用户的可写性。
func TestDoctorOwnerChecksUseServiceUser(t *testing.T) {
	t.Run("root 跑，目录属主是服务用户", func(t *testing.T) {
		p, _ := healthyProbe(t)
		p.currentUser = func() (string, bool) { return "root", true }
		p.serviceUser = "yiban"
		p.dirStat = func(string) (dirInfo, bool) {
			return dirInfo{owner: "yiban", group: "yiban", perm: 0o700}, true
		}
		rep := runChecks(context.Background(), p)
		for _, name := range []string{"fs.photo_dir", "fs.photo_owner"} {
			if row := rowOf(t, rep, name); row.fail {
				t.Fatalf("%s 不得因 root 自己可写而 FAIL：%s", name, row.detail)
			}
		}
	})

	t.Run("目录属主是 postgres", func(t *testing.T) {
		p, _ := healthyProbe(t)
		p.currentUser = func() (string, bool) { return "root", true }
		p.serviceUser = "yiban"
		p.dirStat = func(string) (dirInfo, bool) {
			return dirInfo{owner: "postgres", group: "postgres", perm: 0o700}, true
		}
		rep := runChecks(context.Background(), p)
		for _, name := range []string{"fs.photo_dir", "fs.photo_owner"} {
			row := rowOf(t, rep, name)
			if !row.fail {
				t.Fatalf("%s 必须 FAIL（服务用户 yiban 不可写）: %s", name, row.detail)
			}
			if !strings.Contains(row.fix, "chown") {
				t.Fatalf("%s 的修复必须是 chown：%q", name, row.fix)
			}
		}
	})

	t.Run("组写位且服务用户在组内", func(t *testing.T) {
		p, _ := healthyProbe(t)
		p.currentUser = func() (string, bool) { return "root", true }
		p.serviceUser = "yiban"
		p.dirStat = func(string) (dirInfo, bool) {
			return dirInfo{owner: "root", group: "yiban", perm: 0o770}, true
		}
		rep := runChecks(context.Background(), p)
		if row := rowOf(t, rep, "fs.photo_dir"); row.fail {
			t.Fatalf("组写位有效时不得 FAIL：%s", row.detail)
		}
	})
}

// TestDefaultRegistryResolvesInstalledPath 钉住名册缺省查找顺序（派单 F8）。
// 生产不在仓库根跑 doctor：先找安装位 /etc/yiban/registry.json。
func TestDefaultRegistryResolvesInstalledPath(t *testing.T) {
	// 查找顺序本身也是判据：安装位必须排第一，仓库相对路径兜底。
	if cands := registryCandidates(); len(cands) == 0 || cands[0] != "/etc/yiban/registry.json" {
		t.Fatalf("registryCandidates() = %v，首位必须是安装位 /etc/yiban/registry.json", cands)
	}
	dir := t.TempDir()
	installed := filepath.Join(dir, "etc-yiban-registry.json")
	if err := os.WriteFile(installed, []byte(`{"_meta":{}}`), 0o644); err != nil {
		t.Fatalf("write installed registry: %v", err)
	}
	missing := filepath.Join(dir, "missing.json")
	got := pickRegistry("", func(p string) bool {
		_, err := os.Stat(p)
		return err == nil
	}, missing, installed, "config/registry.json")
	if got != installed {
		t.Fatalf("pickRegistry = %q，want %q（首个存在的候选）", got, installed)
	}
	if got := pickRegistry("/explicit/path.json", func(string) bool { return false }, installed); got != "/explicit/path.json" {
		t.Fatalf("显式 --registry 必须优先，got %q", got)
	}
	// 全不存在时返回最后一个候选：SKIP 文案要点名它。
	got = pickRegistry("", func(string) bool { return false }, missing, "config/registry.json")
	if got != "config/registry.json" {
		t.Fatalf("全部缺失时返回 = %q，want 末位候选", got)
	}
}

// TestServiceUserDefaultFollowsSystemdUnit 钉住服务用户缺省判据（派单 P1-1）。
// 有 systemd 单元 ⇒ 服务以 User=yiban 运行；无单元 ⇒ 本进程就是服务身份
// （容器与前台运行都走这条），属主判据必须按当前用户判。
func TestServiceUserDefaultFollowsSystemdUnit(t *testing.T) {
	origUnit, origUser := unitFileExists, currentUserLookup
	t.Cleanup(func() { unitFileExists, currentUserLookup = origUnit, origUser })
	currentUserLookup = func() (string, bool) { return "nonroot", true }

	unitFileExists = func(string) bool { return false }
	if got := defaultServiceUserResolved(); got != "nonroot" {
		t.Fatalf("无 systemd 单元时缺省服务用户 = %q，want 当前用户 nonroot", got)
	}

	unitFileExists = func(string) bool { return true }
	if got := defaultServiceUserResolved(); got != defaultServiceUser {
		t.Fatalf("有 systemd 单元时缺省服务用户 = %q，want %q", got, defaultServiceUser)
	}

	// 拿不到当前用户名时回退到单元口径：宁可按 yiban 报，也不静默放宽。
	unitFileExists = func(string) bool { return false }
	currentUserLookup = func() (string, bool) { return "", false }
	if got := defaultServiceUserResolved(); got != defaultServiceUser {
		t.Fatalf("拿不到当前用户时缺省服务用户 = %q，want %q", got, defaultServiceUser)
	}
}

// TestRunDoctorResolvesServiceUserDefault 钉住 runDoctor 的缺省真的经判据解析。
// 只测纯函数会漏掉「入口写死 yiban」的回归：容器里那会把无害故障报成 FAIL。
func TestRunDoctorResolvesServiceUserDefault(t *testing.T) {
	me, ok := currentUserName()
	if !ok {
		t.Skip("SKIP: 当前平台拿不到用户名，服务用户缺省无法观察")
	}
	dir := t.TempDir()
	origUnit, origUser := unitFileExists, currentUserLookup
	t.Cleanup(func() { unitFileExists, currentUserLookup = origUnit, origUser })
	currentUserLookup = func() (string, bool) { return me, true }

	photoDirLine := func() string {
		t.Helper()
		var out strings.Builder
		runDoctor([]string{"--photo-dir", dir}, &out, io.Discard)
		for _, line := range strings.Split(out.String(), "\n") {
			if strings.Contains(line, "fs.photo_dir") {
				return line
			}
		}
		t.Fatalf("doctor 输出缺 fs.photo_dir:\n%s", out.String())
		return ""
	}

	t.Run("无 systemd 单元取当前用户", func(t *testing.T) {
		unitFileExists = func(string) bool { return false }
		if line := photoDirLine(); !strings.Contains(line, "服务用户 "+me) {
			t.Fatalf("没有 systemd 单元时服务用户必须是当前用户 %q：%s", me, line)
		}
	})
	t.Run("有 systemd 单元仍按 yiban", func(t *testing.T) {
		unitFileExists = func(string) bool { return true }
		if line := photoDirLine(); !strings.Contains(line, "服务用户 "+defaultServiceUser) {
			t.Fatalf("有 systemd 单元时服务用户必须是 %q：%s", defaultServiceUser, line)
		}
	})
}

// TestDoctorContainerPhotoFixUsesHostCommand 钉住容器内的修复提示（派单 P1-1）。
// distroless 没有 shell 与 chown：容器内打印 `chown yiban` 等于给一条
// 不可执行的命令。修复必须在宿主机执行，或指向镜像预建的属主。
func TestDoctorContainerPhotoFixUsesHostCommand(t *testing.T) {
	p, _ := healthyProbe(t)
	p.containerized = true
	p.currentUser = func() (string, bool) { return "root", true }
	p.serviceUser = "nonroot"
	p.dirStat = func(string) (dirInfo, bool) {
		return dirInfo{owner: "root", group: "root", perm: 0o700}, true
	}
	rep := runChecks(context.Background(), p)
	for _, name := range []string{"fs.photo_dir", "fs.photo_owner"} {
		row := rowOf(t, rep, name)
		if !row.fail {
			t.Fatalf("%s 必须 FAIL（属主 root，服务用户 nonroot 不可写）: %s", name, row.detail)
		}
		for _, bad := range []string{"chown -R nonroot", "chown nonroot", "chown yiban"} {
			if strings.Contains(row.fix, bad) {
				t.Fatalf("%s 给了容器内不可执行的修复 %q：%q", name, bad, row.fix)
			}
		}
		if !strings.Contains(row.fix, "宿主机") || !strings.Contains(row.fix, "docker run") {
			t.Fatalf("%s 的修复必须在宿主机执行：%q", name, row.fix)
		}
	}
}
