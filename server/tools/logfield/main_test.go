package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// writeFixture 在临时目录里放一个源文件。
func writeFixture(t *testing.T, dir, name, src string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(src), 0o600); err != nil {
		t.Fatalf("write fixture %s: %v", name, err)
	}
}

// runFixture 用单文件夹具跑一次门，返回 rc 与输出。
func runFixture(t *testing.T, src string) (int, string, string) {
	t.Helper()
	dir := t.TempDir()
	writeFixture(t, dir, "fixture.go", src)
	var stdout, stderr bytes.Buffer
	rc := run(dir, &stdout, &stderr)
	return rc, stdout.String(), stderr.String()
}

// 干净夹具：键全部走常量，Log 的键位从第 4 个实参起，
// err.Error() 与 entry.Info() 不是日志调用，不得误报。
func TestRunAcceptsLogFieldConstants(t *testing.T) {
	const src = `package fixture

func f(logger interface{}, ctx context.Context, level any, err error, entry DirEntry) {
	attrs := []any{logfield.KeyError, err.Error(), logfield.KeyCount, 1}
	logger.Info("msg", attrs...)
	logger.Log(ctx, level, "msg", logfield.KeyDay, "2026-11-03")
	_ = err.Error()
	entry.Info()
}
`
	rc, stdout, stderr := runFixture(t, src)
	if rc != 0 {
		t.Fatalf("rc = %d, want 0; stdout=%s stderr=%s", rc, stdout, stderr)
	}
	if !strings.Contains(stdout, "logfield: ok") {
		t.Fatalf("stdout = %q, want ok line", stdout)
	}
}

// 裸字符串键必须红，即使值就在表内（"error"）。
func TestRunRejectsBareStringKey(t *testing.T) {
	const src = `package fixture

func f(logger interface{}, err error) {
	logger.Error("msg", "error", err)
}
`
	rc, stdout, _ := runFixture(t, src)
	if rc != 1 {
		t.Fatalf("rc = %d, want 1", rc)
	}
	if !strings.Contains(stdout, "字符串字面量") || !strings.Contains(stdout, `"error"`) {
		t.Fatalf("stdout = %q, want bare literal violation", stdout)
	}
}

// 未知常量必须红：表外名字不能被当成合法键。
func TestRunRejectsUnknownConstant(t *testing.T) {
	const src = `package fixture

func f(logger interface{}) {
	logger.Info("msg", logfield.KeyNope, 1)
}
`
	rc, stdout, _ := runFixture(t, src)
	if rc != 1 {
		t.Fatalf("rc = %d, want 1", rc)
	}
	if !strings.Contains(stdout, "KeyNope") {
		t.Fatalf("stdout = %q, want unknown constant violation", stdout)
	}
}

// 转发切片里的裸键同样要红：只会出现在 []any{...} 的偶数位。
func TestRunRejectsBareKeyInAttrsSlice(t *testing.T) {
	const src = `package fixture

func f(logger interface{}, err error) {
	attrs := []any{"error", err.Error()}
	logger.Info("msg", attrs...)
}
`
	rc, stdout, _ := runFixture(t, src)
	if rc != 1 {
		t.Fatalf("rc = %d, want 1", rc)
	}
	if !strings.Contains(stdout, `"error"`) {
		t.Fatalf("stdout = %q, want attrs literal violation", stdout)
	}
}

// 转发参数无 allow 必须红；加了带理由的 allow 则绿。
func TestRunRequiresAllowForForwardedParams(t *testing.T) {
	const base = `package fixture

func (l cronLog) Info(msg string, keysAndValues ...any) {
%s	l.log.Info(msg, keysAndValues...)
}

func (l cronLog) Error(err error, msg string, keysAndValues ...any) {
	args := append([]any{logfield.KeyError, err.Error()}, keysAndValues...)
	l.log.Error(msg, args...)
}
`
	red := strings.Replace(base, "%s", "", 1)
	rc, stdout, stderr := runFixture(t, red)
	if rc != 1 {
		t.Fatalf("red rc = %d, want 1; stdout=%s stderr=%s", rc, stdout, stderr)
	}
	if !strings.Contains(stdout, "logfield:allow") {
		t.Fatalf("red stdout = %q, want allow violation", stdout)
	}

	green := strings.Replace(base, "%s", "\t//logfield:allow robfig/cron 透传键\n", 1)
	rc, stdout, stderr = runFixture(t, green)
	if rc != 0 {
		t.Fatalf("green rc = %d, want 0; stdout=%s stderr=%s", rc, stdout, stderr)
	}
	if !strings.Contains(stdout, "logfield: ok") {
		t.Fatalf("green stdout = %q, want ok line", stdout)
	}
}

// 空目录不得当绿：0 调用即拒绝。
func TestRunRejectsEmptyTree(t *testing.T) {
	dir := t.TempDir()
	var stdout, stderr bytes.Buffer
	rc := run(dir, &stdout, &stderr)
	if rc != 2 {
		t.Fatalf("rc = %d, want 2", rc)
	}
	if !strings.Contains(stderr.String(), "no log calls") {
		t.Fatalf("stderr = %q, want empty-scan refusal", stderr.String())
	}
}
