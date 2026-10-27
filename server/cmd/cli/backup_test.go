package main

// backup/restore 的单元测试。
//
// # 功能
// 参数校验、工具缺失的显性失败、外部命令参数形状、
// manifest 注入与解析、行数对账的成败两路。
// 全部经 cmdRunner/lookPath/countRows 注入，本文件**不**连真库、
// **不**依赖 pg_dump/gpg。
//
// # 口径
// 判据来自派单 P7：pg_dump 不可用时报错不装绿；gpg 缺失时报错；
// restore 需 --force 或交互确认；恢复后打印行数对账表。

import (
	"bytes"
	"context"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// fakeRunner 记录调用并按序回放脚本。
type fakeRunner struct {
	calls []fakeCall
	// script 按调用序返回；超出长度返回 (nil, nil)。
	script []fakeRunResult
	// captured 存下 captureIn 读取的文件内容，供断言。
	captured map[string][]byte
}

type fakeCall struct {
	name  string
	args  []string
	stdin []byte
	env   []string
	// passfile 是本次调用时 PGPASSFILE 指向文件的内容与权限（缺失时为空）。
	passfilePath string
	passfileBody []byte
	passfileMode os.FileMode
}

type fakeRunResult struct {
	out []byte
	err error
	// writeOut 非空时写入本次调用 --output 指向的路径（模拟 gpg 产出）。
	writeOut string
	// captureIn 读取本次调用的输入文件（--file= 的值，否则末位位置参数）
	// 内容存进 captured（模拟读取转储）。
	captureIn bool
}

func (f *fakeRunner) Run(_ context.Context, name string, args []string, stdin []byte, env []string) ([]byte, error) {
	call := fakeCall{name: name, args: args, stdin: stdin, env: env}
	// 口令文件必须在子进程运行期间存在且 0600：调用时读取，事后无法伪造。
	for _, kv := range env {
		if path, ok := strings.CutPrefix(kv, "PGPASSFILE="); ok {
			call.passfilePath = path
			if info, err := os.Stat(path); err == nil {
				call.passfileMode = info.Mode().Perm()
			}
			if body, err := os.ReadFile(path); err == nil {
				call.passfileBody = body
			}
		}
	}
	f.calls = append(f.calls, call)
	if len(f.script) > 0 {
		r := f.script[0]
		f.script = f.script[1:]
		if r.writeOut != "" {
			out, ok := outputFileOf(call)
			if !ok {
				return nil, errors.New("fakeRunner: no --output in args")
			}
			if err := os.WriteFile(out, []byte(r.writeOut), 0o600); err != nil {
				return nil, err
			}
		}
		if r.captureIn {
			in, ok := inputFileOf(call)
			if !ok {
				return nil, errors.New("fakeRunner: no input file in args")
			}
			body, err := os.ReadFile(in)
			if err != nil {
				return nil, err
			}
			if f.captured == nil {
				f.captured = map[string][]byte{}
			}
			f.captured[in] = body
		}
		return r.out, r.err
	}
	return nil, nil
}

// outputFileOf 取 args 里 --output 的值。
func outputFileOf(c fakeCall) (string, bool) { return valueOf(c.args, "--output") }

// inputFileOf 取输入文件：--file= 前缀的值，否则末位位置参数。
func inputFileOf(c fakeCall) (string, bool) {
	if v, ok := valueOf(c.args, "--file"); ok {
		return v, true
	}
	for i := len(c.args) - 1; i >= 0; i-- {
		if !strings.HasPrefix(c.args[i], "--") {
			return c.args[i], true
		}
	}
	return "", false
}

func valueOf(args []string, flag string) (string, bool) {
	for _, a := range args {
		if v, ok := strings.CutPrefix(a, flag+"="); ok {
			return v, true
		}
	}
	return "", false
}

func (f *fakeRunner) callsOf(name string) []fakeCall {
	var out []fakeCall
	for _, c := range f.calls {
		if c.name == name || strings.HasSuffix(c.name, "/"+name) {
			out = append(out, c)
		}
	}
	return out
}

func okLookPath(name string) (string, error) { return "/usr/bin/" + name, nil }

func missingLookPath(tool string) func(string) (string, error) {
	return func(name string) (string, error) {
		if name == tool {
			return "", errors.New("not found")
		}
		return "/usr/bin/" + name, nil
	}
}

// newTestDeps 是可用的基线：全部替身就绪，个别用例再覆写。
func newTestDeps(t *testing.T) (*backupDeps, *fakeRunner, *bytes.Buffer, *bytes.Buffer) {
	t.Helper()
	runner := &fakeRunner{}
	stdout := &bytes.Buffer{}
	stderr := &bytes.Buffer{}
	return &backupDeps{
		stdout:    stdout,
		stderr:    stderr,
		lookPath:  okLookPath,
		runner:    runner,
		countRows: func(context.Context, string) (map[string]int64, error) { return nil, nil },
		migrate:   func(context.Context, string) (int64, error) { return 16, nil },
		tty:       func() bool { return false },
		confirm:   func() bool { return false },
	}, runner, stdout, stderr
}

const testDSN = "postgres://u:p@127.0.0.1:5432/db?search_path=bkpt_x"

// passFileOf 建一个 0600 口令文件供用例使用。
func passFileOf(t *testing.T) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "pass")
	if err := os.WriteFile(p, []byte("secret"), 0o600); err != nil {
		t.Fatalf("write pass: %v", err)
	}
	return p
}

func contains(args []string, want string) bool {
	for _, a := range args {
		if a == want {
			return true
		}
	}
	return false
}

// containsSub 判 args 里是否有含 sub 的项。
func containsSub(args []string, sub string) bool {
	for _, a := range args {
		if strings.Contains(a, sub) {
			return true
		}
	}
	return false
}

func stderrOf(deps *backupDeps) string { return deps.stderr.(*bytes.Buffer).String() }

func TestBackupRejectsMissingPgDump(t *testing.T) {
	deps, _, _, stderr := newTestDeps(t)
	deps.lookPath = missingLookPath("pg_dump")
	code := runBackup(deps, []string{"--out", filepath.Join(t.TempDir(), "b.gpg"), "--dsn", testDSN})
	if code != 1 {
		t.Fatalf("code = %d, want 1", code)
	}
	if !strings.Contains(stderr.String(), "pg_dump") {
		t.Fatalf("stderr must name pg_dump: %q", stderr.String())
	}
}

func TestBackupRejectsMissingGpg(t *testing.T) {
	deps, _, _, stderr := newTestDeps(t)
	deps.lookPath = missingLookPath("gpg")
	code := runBackup(deps, []string{"--out", filepath.Join(t.TempDir(), "b.gpg"), "--dsn", testDSN})
	if code != 1 {
		t.Fatalf("code = %d, want 1", code)
	}
	if !strings.Contains(stderr.String(), "gpg") {
		t.Fatalf("stderr must name gpg: %q", stderr.String())
	}
}

func TestBackupRejectsMissingPassphrase(t *testing.T) {
	deps, _, _, stderr := newTestDeps(t)
	code := runBackup(deps, []string{"--out", filepath.Join(t.TempDir(), "b.gpg"), "--dsn", testDSN})
	if code != 1 {
		t.Fatalf("code = %d, want 1", code)
	}
	if !strings.Contains(stderr.String(), "passphrase") {
		t.Fatalf("stderr must name passphrase: %q", stderr.String())
	}
}

// TestBackupRejectsLoosePassphraseFile 钉住口令文件必须 0600（派单 F10）。
// Windows 的 FileMode 权限位是合成的（恒 0666），POSIX 判据无判别力：
// 本用例在 Windows 显性跳过，门禁在 Linux/WSL 上执行。
func TestBackupRejectsLoosePassphraseFile(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("SKIP: Windows 的 FileMode 权限位是合成的（0666），0600 判据无判别力；Linux/WSL 上有牙")
	}
	deps, _, _, stderr := newTestDeps(t)
	p := filepath.Join(t.TempDir(), "pass")
	if err := os.WriteFile(p, []byte("secret"), 0o644); err != nil {
		t.Fatalf("write pass: %v", err)
	}
	if err := os.Chmod(p, 0o644); err != nil {
		t.Fatalf("chmod pass: %v", err)
	}
	code := runBackup(deps, []string{"--out", filepath.Join(t.TempDir(), "b.gpg"),
		"--dsn", testDSN, "--passphrase-file", p})
	if code != 1 {
		t.Fatalf("code = %d, want 1（0644 口令文件必须拒绝）", code)
	}
	if !strings.Contains(stderr.String(), "0600") {
		t.Fatalf("stderr must name the 0600 requirement: %q", stderr.String())
	}
}

// TestBackupKeepsDSNOutOfArgv 钉住口令不进 argv（派单 F9）。
// 判据：pg_dump 的 argv 不含口令、不含 --dbname=<DSN>；
// 环境带 PGPASSFILE，文件在子进程运行期存在、0600、内容正确；
// 备份结束后临时口令文件已删除。
func TestBackupKeepsDSNOutOfArgv(t *testing.T) {
	const dsn = "postgres://yiban:s3cr3t@db.example:5433/yiban?sslmode=disable"
	deps, runner, _, _ := newTestDeps(t)
	out := filepath.Join(t.TempDir(), "b.gpg")
	runner.script = []fakeRunResult{{}, {}}
	code := runBackup(deps, []string{"--out", out, "--dsn", dsn, "--passphrase-file", passFileOf(t)})
	if code != 0 {
		t.Fatalf("code = %d, stderr=%q", code, stderrOf(deps))
	}
	dumps := runner.callsOf("pg_dump")
	if len(dumps) != 1 {
		t.Fatalf("pg_dump calls = %d, want 1", len(dumps))
	}
	call := dumps[0]
	if containsSub(call.args, "s3cr3t") {
		t.Fatalf("argv 含口令：%v", call.args)
	}
	if containsSub(call.args, "--dbname=") {
		t.Fatalf("argv 不得带 --dbname=<DSN>：%v", call.args)
	}
	// 连接参数改为分项：-h/-p/-U/-d。
	for _, pair := range [][2]string{{"-h", "db.example"}, {"-p", "5433"}, {"-U", "yiban"}, {"-d", "yiban"}} {
		i := indexOf(call.args, pair[0])
		if i < 0 || i+1 >= len(call.args) || call.args[i+1] != pair[1] {
			t.Fatalf("argv 缺 %s %s：%v", pair[0], pair[1], call.args)
		}
	}
	if call.passfilePath == "" {
		t.Fatalf("环境缺 PGPASSFILE：%v", call.env)
	}
	// Windows 的 FileMode 是合成值，不支持 POSIX 权限位；该断言只在 POSIX 生效。
	if runtime.GOOS == "windows" {
		t.Log("SKIP: Windows FileMode 不携带 POSIX 权限位，0600 断言不适用")
	} else if call.passfileMode != 0o600 {
		t.Fatalf("PGPASSFILE 权限 = %04o，want 0600", call.passfileMode)
	}
	if want := "db.example:5433:yiban:yiban:s3cr3t\n"; string(call.passfileBody) != want {
		t.Fatalf("PGPASSFILE 内容 = %q，want %q", call.passfileBody, want)
	}
	if _, err := os.Stat(call.passfilePath); !os.IsNotExist(err) {
		t.Fatalf("备份结束后临时口令文件必须删除：%v", err)
	}
}

// indexOf 返回 args 里第一个等于 want 的下标。
func indexOf(args []string, want string) int {
	for i, a := range args {
		if a == want {
			return i
		}
	}
	return -1
}

func TestBackupRequiresOutAndDSN(t *testing.T) {
	deps, _, _, _ := newTestDeps(t)
	if code := runBackup(deps, nil); code != 2 {
		t.Fatalf("no args: code = %d, want 2", code)
	}
	if code := runBackup(deps, []string{"--out", "x.gpg"}); code != 2 {
		t.Fatalf("no dsn: code = %d, want 2", code)
	}
	if code := runBackup(deps, []string{"--dsn", testDSN}); code != 2 {
		t.Fatalf("no out: code = %d, want 2", code)
	}
}

func TestBackupRunsScopedDumpWithManifest(t *testing.T) {
	deps, runner, stdout, _ := newTestDeps(t)
	out := filepath.Join(t.TempDir(), "b.gpg")
	runner.script = []fakeRunResult{
		{},                                    // pg_dump
		{captureIn: true, writeOut: "cipher"}, // gpg：产出密文并抓取转储
	}
	deps.countRows = func(context.Context, string) (map[string]int64, error) {
		return map[string]int64{"users": 3, "accounts": 5}, nil
	}
	code := runBackup(deps, []string{"--out", out, "--dsn", testDSN, "--passphrase-file", passFileOf(t)})
	if code != 0 {
		t.Fatalf("code = %d, stderr=%q", code, stderrOf(deps))
	}

	dumps := runner.callsOf("pg_dump")
	if len(dumps) != 1 {
		t.Fatalf("pg_dump calls = %d, want 1", len(dumps))
	}
	args := dumps[0].args
	for _, want := range []string{"--no-owner", "--no-privileges", "-n", "bkpt_x"} {
		if !contains(args, want) {
			t.Fatalf("pg_dump args missing %s: %v", want, args)
		}
	}
	// search_path 只进 -n 的 schema 参数，不进连接参数。
	if i := indexOf(args, "-d"); i < 0 || i+1 >= len(args) || args[i+1] != "db" {
		t.Fatalf("pg_dump 的库名参数应为 -d db（不带 search_path）：%v", args)
	}
	dumpPath, _ := valueOf(args, "--file")
	body := string(runner.captured[dumpPath])
	if !strings.Contains(body, manifestMarker) {
		t.Fatalf("dump must carry the manifest marker: %q", body)
	}
	if !strings.Contains(body, `"users":3`) {
		t.Fatalf("manifest must carry user count: %q", body)
	}

	gpg := runner.callsOf("gpg")
	if len(gpg) != 1 {
		t.Fatalf("gpg calls = %d, want 1", len(gpg))
	}
	if !contains(gpg[0].args, "--symmetric") || !contains(gpg[0].args, "--output="+out) {
		t.Fatalf("gpg must encrypt symmetrically into %s: %v", out, gpg[0].args)
	}
	if len(gpg[0].stdin) == 0 {
		t.Fatal("gpg must receive the passphrase on stdin")
	}
	if !strings.Contains(stdout.String(), out) {
		t.Fatalf("stdout must name the archive: %q", stdout.String())
	}
}

func TestRestoreRefusesWithoutForceOnNonTTY(t *testing.T) {
	deps, _, _, stderr := newTestDeps(t)
	deps.tty = func() bool { return false }
	code := runRestore(deps, []string{"--in", "b.gpg", "--dsn", testDSN})
	if code != 1 {
		t.Fatalf("code = %d, want 1", code)
	}
	if !strings.Contains(stderr.String(), "--force") {
		t.Fatalf("stderr must point at --force: %q", stderr.String())
	}
}

func TestRestoreAsksForConfirmationOnTTY(t *testing.T) {
	deps, _, _, _ := newTestDeps(t)
	deps.tty = func() bool { return true }
	asked := false
	deps.confirm = func() bool { asked = true; return false } // 拒绝确认
	code := runRestore(deps, []string{"--in", "b.gpg", "--dsn", testDSN})
	if code != 1 {
		t.Fatalf("code = %d, want 1", code)
	}
	if !asked {
		t.Fatal("TTY 路径必须先征询确认")
	}
}

func TestRestoreMissingManifestFails(t *testing.T) {
	deps, runner, _, _ := newTestDeps(t)
	out := filepath.Join(t.TempDir(), "b.gpg")
	runner.script = []fakeRunResult{
		{writeOut: "-- 只有一条普通注释，没有清单\n"},
		{},
	}
	code := runRestore(deps, []string{"--in", out, "--dsn", testDSN, "--force", "--passphrase-file", passFileOf(t)})
	if code != 1 {
		t.Fatalf("code = %d, want 1 without manifest", code)
	}
}

func TestRestoreMismatchFails(t *testing.T) {
	deps, runner, stdout, _ := newTestDeps(t)
	out := filepath.Join(t.TempDir(), "b.gpg")
	runner.script = []fakeRunResult{
		{writeOut: manifestMarker + ` {"users":3,"accounts":5}` + "\n"}, // gpg 解密产物
		{}, // psql
	}
	deps.countRows = func(context.Context, string) (map[string]int64, error) {
		// 恢复后的实数与 manifest 不一致。
		return map[string]int64{"users": 2, "accounts": 5}, nil
	}
	code := runRestore(deps, []string{"--in", out, "--dsn", testDSN, "--force", "--passphrase-file", passFileOf(t)})
	if code != 1 {
		t.Fatalf("code = %d, want 1 on mismatch", code)
	}
	if !strings.Contains(stdout.String(), "users") || !strings.Contains(stdout.String(), "3") {
		t.Fatalf("stdout must show the reconciliation table: %q", stdout.String())
	}
	if !strings.Contains(stdout.String(), "不一致") {
		t.Fatalf("stdout must flag the mismatch: %q", stdout.String())
	}
}

func TestRestoreSuccessReconciles(t *testing.T) {
	deps, runner, stdout, _ := newTestDeps(t)
	out := filepath.Join(t.TempDir(), "b.gpg")
	runner.script = []fakeRunResult{
		{writeOut: "-- dump\n" + manifestMarker + ` {"users":3,"accounts":5}` + "\n"},
		{},
	}
	migrated := false
	deps.migrate = func(context.Context, string) (int64, error) { migrated = true; return 16, nil }
	deps.countRows = func(context.Context, string) (map[string]int64, error) {
		return map[string]int64{"users": 3, "accounts": 5}, nil
	}
	code := runRestore(deps, []string{"--in", out, "--dsn", testDSN, "--force", "--passphrase-file", passFileOf(t)})
	if code != 0 {
		t.Fatalf("code = %d, stderr=%q", code, stderrOf(deps))
	}
	if !migrated {
		t.Fatal("restore 必须在导入后跑迁移")
	}
	if !strings.Contains(stdout.String(), "一致") {
		t.Fatalf("stdout must report reconciliation: %q", stdout.String())
	}
}

// TestManifestTablesCoverEveryBusinessTable 钉住对账表的完整性（派单 F17）。
// 判据源是 doctor 的 expectedTables（迁移 00001–00016 的表全集）：
// 对账表必须覆盖它，且不得有重复。
func TestManifestTablesCoverEveryBusinessTable(t *testing.T) {
	have := map[string]bool{}
	for _, table := range manifestTables {
		if have[table] {
			t.Fatalf("manifestTables 有重复项 %q", table)
		}
		have[table] = true
	}
	for _, table := range expectedTables {
		if !have[table] {
			t.Fatalf("manifestTables 缺业务表 %q：备份对账不覆盖它，恢复后丢行不会被发现", table)
		}
		if reason, exempt := manifestExemptTables[table]; exempt {
			t.Fatalf("业务表 %q 不得豁免（豁免理由 %q）", table, reason)
		}
	}
	// 豁免面必须非空且每条带理由：空豁免也要能解释。
	for table, reason := range manifestExemptTables {
		if strings.TrimSpace(reason) == "" {
			t.Fatalf("豁免表 %q 没有理由", table)
		}
	}
}

// TestParseKeywordDSNBackslashEscapes 钉住 libpq 引号内转义（派单 P2-5）。
// 引号内反斜杠转义下一字符；两个相邻单引号也是一个字面单引号。
// 不认转义会把后续字段吞进口令，或把残值当口令（凭据错）。
func TestParseKeywordDSNBackslashEscapes(t *testing.T) {
	// bs 用码点构造：反斜杠不经过源码转义层，读起来就是它本身。
	bs := string(rune(92))
	cases := []struct{ name, dsn, password, dbname string }{
		{"escaped-quote", "host=db user=u password='a" + bs + "'b' dbname=keep", "a'b", "keep"},
		{"doubled-quote", `host=db user=u password='a''b' dbname=keep`, "a'b", "keep"},
		{"escaped-backslash", "host=db user=u password='a" + bs + bs + "b' dbname=keep", "a" + bs + "b", "keep"},
		{"spaced-values", `host=db user=u password = 'a b' dbname=keep`, "a b", "keep"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			p, err := parseDSN(tc.dsn)
			if err != nil {
				t.Fatalf("parseDSN(%q): %v", tc.dsn, err)
			}
			if p.password != tc.password {
				t.Fatalf("password = %q, want %q", p.password, tc.password)
			}
			if p.dbname != tc.dbname {
				t.Fatalf("dbname = %q, want %q：后续字段被吞进口令", p.dbname, tc.dbname)
			}
		})
	}
	// 未闭合引号必须失败关闭：不能把残值当口令。
	if _, err := parseDSN(`host=db password='oops`); err == nil {
		t.Fatal("未闭合引号必须报错")
	}
}

// TestManifestExemptionsAreWhitelisted 钉住豁免面（派单 P2-6）。
// 只准迁移工具簿记表：新业务表写条理由就双绿是缺陷形状。
func TestManifestExemptionsAreWhitelisted(t *testing.T) {
	for table, reason := range manifestExemptTables {
		if !manifestExemptWhitelist[table] {
			t.Fatalf("表 %q 已豁免（理由 %q）但不在白名单：业务表不得豁免", table, reason)
		}
	}
}
