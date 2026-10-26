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
		now:      func() time.Time { return base },
		tryListen: func(string) (closer, error) {
			return nopCloser{}, nil
		},
		httpGet: func(string, string) (int, error) { return 0, errors.New("no server") },
		openDB: func(context.Context, string, []byte) (dbHandle, error) {
			return db, nil
		},
		diskFree: func(string) (uint64, error) { return 8 << 30, nil },
		dirOwner: func(string) (string, bool) { return currentUserNameForTest(t), true },
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

func TestDoctorFaultInjectionHitsEachCheck(t *testing.T) {
	// 每条一例：注入一种故障，断言该条 FAIL 且给出修复命令。
	cases := []struct {
		name        string
		inject      func(p *probe, db *fakeDB)
		skipWindows string // 非 Windows 才能注入时写明原因
	}{
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
			p.src = fakeSource{"YIBAN_ENV": "production", "YIBAN_HASH_PEPPER": testPepper, "YIBAN_CREDENTIAL_KEY": testKeyB64, "YIBAN_TZ": "UTC", "YIBAN_TYPO_KEY": "1"}
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
		}, ""},
		{"fs.disk", func(p *probe, _ *fakeDB) {
			p.diskFree = func(string) (uint64, error) { return 64 << 20, nil } // 64 MiB
		}, ""},
		{"fs.photo_owner", func(p *probe, _ *fakeDB) { p.dirOwner = func(string) (string, bool) { return "root", true } },
			"current user is root; owner mismatch cannot be constructed"},
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
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if tc.skipWindows != "" && runtime.GOOS == "windows" {
				t.Skip("SKIP: " + tc.skipWindows)
			}
			if tc.name == "fs.photo_owner" {
				if me, ok := currentUserName(); !ok {
					t.Skip("SKIP: 当前平台拿不到用户名，属主故障注入不了")
				} else if me == "root" {
					// root 运行时把属主改成 root 与当前用户仍是同一人，
					// 属主失配这个故障构造不出来。
					t.Skip("SKIP: running as root; owner mismatch cannot be constructed")
				}
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
