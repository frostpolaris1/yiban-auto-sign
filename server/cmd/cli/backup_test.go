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

func (f *fakeRunner) Run(_ context.Context, name string, args []string, stdin []byte) ([]byte, error) {
	call := fakeCall{name: name, args: args, stdin: stdin}
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
	if dbName, _ := valueOf(args, "--dbname"); strings.Contains(dbName, "search_path") {
		t.Fatalf("--dbname must not carry search_path (libpq rejects it): %q", dbName)
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
