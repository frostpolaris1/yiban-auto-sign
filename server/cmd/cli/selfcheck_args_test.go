package main

// selfcheck 子命令的参数与输出判据。
//
// # 口径
// 注入 lookup 与 deps，不读真实环境、不连真库；
// 真库 schema 版本读取另见 selfcheck_pg_e2e_test.go。
// --json 只输出 JSON；人读输出不含 JSON 字段；--record-keys 是唯一写路径。

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app/updatecheck"
)

const (
	testCLICredentialKey = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
	testCLIPepper        = "0123456789abcdef0123456789abcdef"
)

// cliFakeDB 实现 updatecheck.DB。
type cliFakeDB struct {
	version int64
	err     error
}

func (d *cliFakeDB) SchemaVersion(context.Context) (int64, error) { return d.version, d.err }

func (d *cliFakeDB) Close() {}

// selfcheckFixture 是一组不依赖真实环境的输入。
type selfcheckFixture struct {
	env  map[string]string
	deps updatecheck.Deps
	dir  string
}

// newSelfcheckFixture 建齐 UI、照片、备份目录。
func newSelfcheckFixture(t *testing.T, dbVersion int64) *selfcheckFixture {
	t.Helper()
	dir := t.TempDir()
	ui := filepath.Join(dir, "ui")
	photos := filepath.Join(dir, "photos")
	backup := filepath.Join(dir, "backup")
	for _, path := range []string{ui, photos, backup} {
		if err := os.MkdirAll(path, 0o755); err != nil {
			t.Fatalf("mkdir %s: %v", path, err)
		}
	}
	if err := os.WriteFile(filepath.Join(ui, "index.html"), []byte("<!doctype html>"), 0o644); err != nil {
		t.Fatalf("write index: %v", err)
	}
	deps := updatecheck.DefaultDeps()
	deps.Now = func() time.Time { return time.Date(2026, 11, 4, 12, 0, 0, 0, time.UTC) }
	deps.LookPath = func(name string) (string, error) { return "/usr/bin/" + name, nil }
	deps.OpenDB = func(context.Context, string) (updatecheck.DB, error) {
		return &cliFakeDB{version: dbVersion}, nil
	}
	deps.EmbeddedVersion = func() (int64, error) { return dbVersion, nil }
	deps.ServiceProbe = func(context.Context) (bool, bool, string) { return true, true, "active" }
	deps.Build = updatecheck.BuildInfo{Version: "dev", Commit: "unknown", BuildTime: "unknown"}
	return &selfcheckFixture{
		dir:  dir,
		deps: deps,
		env: map[string]string{
			"YIBAN_DATABASE_URL":   "postgres://yiban:s3cret-pass-word@127.0.0.1:5432/yiban?sslmode=disable",
			"YIBAN_HTTP_ADDR":      "127.0.0.1:47183",
			"YIBAN_UI_DIR":         ui,
			"YIBAN_PHOTO_DIR":      photos,
			"YIBAN_CREDENTIAL_KEY": testCLICredentialKey,
			"YIBAN_HASH_PEPPER":    testCLIPepper,
		},
	}
}

// lookup 读夹具环境。
func (f *selfcheckFixture) lookup(key string) string { return f.env[key] }

// statePath 返回临时状态文件路径。
func (f *selfcheckFixture) statePath() string { return filepath.Join(f.dir, "update-state.json") }

// run 调 runSelfcheckWith 并返回退出码与两路输出。
func (f *selfcheckFixture) run(t *testing.T, args ...string) (int, string, string) {
	t.Helper()
	var stdout, stderr bytes.Buffer
	code := runSelfcheckWith(args, &stdout, &stderr, f.lookup, f.deps)
	return code, stdout.String(), stderr.String()
}

func TestSelfcheckUnknownFlagIsUsageError(t *testing.T) {
	f := newSelfcheckFixture(t, 22)
	code, _, stderr := f.run(t, "--nope")
	if code != 2 {
		t.Fatalf("exit = %d, want 2; stderr=%s", code, stderr)
	}
}

func TestSelfcheckPositionalArgIsUsageError(t *testing.T) {
	f := newSelfcheckFixture(t, 22)
	code, _, _ := f.run(t, "extra")
	if code != 2 {
		t.Fatalf("exit = %d, want 2", code)
	}
}

func TestSelfcheckJSONAndRecordKeysConflict(t *testing.T) {
	f := newSelfcheckFixture(t, 22)
	code, _, stderr := f.run(t, "--json", "--record-keys")
	if code != 2 || !strings.Contains(stderr, "不能同时使用") {
		t.Fatalf("exit = %d, stderr=%q", code, stderr)
	}
}

func TestSelfcheckTimeoutMustBePositive(t *testing.T) {
	f := newSelfcheckFixture(t, 22)
	code, _, _ := f.run(t, "--timeout=0s")
	if code != 2 {
		t.Fatalf("exit = %d, want 2", code)
	}
}

func TestSelfcheckJSONOnlyOutput(t *testing.T) {
	f := newSelfcheckFixture(t, 22)
	code, stdout, stderr := f.run(t, "--json", "--state", f.statePath())
	if code != 0 {
		t.Fatalf("exit = %d, want 0; stderr=%s", code, stderr)
	}
	var doc struct {
		SchemaVersion int `json:"schema_version"`
		Checks        []struct {
			Name   string `json:"name"`
			Status string `json:"status"`
		} `json:"checks"`
		Summary struct {
			Pass     int `json:"pass"`
			Fail     int `json:"fail"`
			Skip     int `json:"skip"`
			ExitCode int `json:"exit_code"`
		} `json:"summary"`
	}
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("stdout is not JSON: %v\n%s", err, stdout)
	}
	if doc.SchemaVersion != updatecheck.ReportSchemaVersion || doc.Summary.ExitCode != 0 {
		t.Fatalf("report = %+v", doc)
	}
	if len(doc.Checks) == 0 {
		t.Fatal("no checks in report")
	}
	for _, human := range []string{"[ OK ]", "[FAIL]", "汇总："} {
		if strings.Contains(stdout, human) {
			t.Fatalf("--json leaked human output %q", human)
		}
	}
	if _, err := os.Stat(f.statePath()); !os.IsNotExist(err) {
		t.Fatalf("selfcheck wrote the state file without --record-keys: %v", err)
	}
}

func TestSelfcheckHumanOutput(t *testing.T) {
	f := newSelfcheckFixture(t, 22)
	code, stdout, stderr := f.run(t, "--state", f.statePath())
	if code != 0 {
		t.Fatalf("exit = %d, want 0; stderr=%s", code, stderr)
	}
	if !strings.Contains(stdout, "[ OK ]") || !strings.Contains(stdout, "汇总：") {
		t.Fatalf("human output missing markers:\n%s", stdout)
	}
	if strings.Contains(stdout, `"schema_version"`) {
		t.Fatalf("human output contains JSON:\n%s", stdout)
	}
}

func TestSelfcheckFailureExitCode(t *testing.T) {
	f := newSelfcheckFixture(t, 22)
	delete(f.env, "YIBAN_DATABASE_URL")
	code, stdout, _ := f.run(t, "--state", f.statePath())
	if code != 1 {
		t.Fatalf("exit = %d, want 1", code)
	}
	if !strings.Contains(stdout, "[FAIL] config.dsn") {
		t.Fatalf("missing config.dsn FAIL:\n%s", stdout)
	}
	if !strings.Contains(stdout, "[SKIP] db.connect") {
		t.Fatalf("missing db.connect SKIP:\n%s", stdout)
	}
}

func TestSelfcheckFlagMissingValueIsUsageError(t *testing.T) {
	f := newSelfcheckFixture(t, 22)
	code, _, _ := f.run(t, "--state")
	if code != 2 {
		t.Fatalf("exit = %d, want 2", code)
	}
}

func TestSelfcheckJSONWithFailure(t *testing.T) {
	f := newSelfcheckFixture(t, 22)
	delete(f.env, "YIBAN_DATABASE_URL")
	code, stdout, _ := f.run(t, "--json", "--state", f.statePath())
	if code != 1 {
		t.Fatalf("exit = %d, want 1", code)
	}
	var doc struct {
		Checks []struct {
			Name   string `json:"name"`
			Status string `json:"status"`
		} `json:"checks"`
		Summary struct {
			Fail     int `json:"fail"`
			ExitCode int `json:"exit_code"`
		} `json:"summary"`
	}
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("stdout is not JSON: %v", err)
	}
	if doc.Summary.Fail == 0 || doc.Summary.ExitCode != 1 {
		t.Fatalf("summary = %+v", doc.Summary)
	}
	found := false
	for _, c := range doc.Checks {
		if c.Name == "config.dsn" && c.Status == "FAIL" {
			found = true
		}
	}
	if !found {
		t.Fatalf("config.dsn FAIL missing from %+v", doc.Checks)
	}
	if strings.Contains(stdout, "[FAIL]") {
		t.Fatalf("JSON output contains human rows")
	}
}

func TestSelfcheckRecordKeysWritesState(t *testing.T) {
	f := newSelfcheckFixture(t, 22)
	code, stdout, stderr := f.run(t, "--record-keys", "--state", f.statePath())
	if code != 0 {
		t.Fatalf("exit = %d, want 0; stderr=%s", code, stderr)
	}
	body, err := os.ReadFile(f.statePath())
	if err != nil {
		t.Fatalf("state file not written: %v", err)
	}
	for _, want := range []string{"\"schema_version\": 1", "\"credential_key\"", "\"hash_pepper\"", "\"recorded_at\""} {
		if !strings.Contains(string(body), want) {
			t.Fatalf("state file missing %s:\n%s", want, body)
		}
	}
	for _, secret := range []string{testCLICredentialKey, testCLIPepper} {
		if strings.Contains(string(body), secret) || strings.Contains(stdout, secret) {
			t.Fatalf("secret %q leaked", secret)
		}
	}
	if runtime.GOOS != "windows" {
		info, err := os.Stat(f.statePath())
		if err != nil {
			t.Fatalf("stat state: %v", err)
		}
		if got := info.Mode().Perm(); got != 0o600 {
			t.Fatalf("state perm = %04o, want 0600", got)
		}
	}
}

func TestSelfcheckRecordKeysWithoutKeysFails(t *testing.T) {
	f := newSelfcheckFixture(t, 22)
	delete(f.env, "YIBAN_CREDENTIAL_KEY")
	code, _, stderr := f.run(t, "--record-keys", "--state", f.statePath())
	if code != 1 || !strings.Contains(stderr, "CREDENTIAL_KEY") {
		t.Fatalf("exit = %d, stderr=%q", code, stderr)
	}
	if _, err := os.Stat(f.statePath()); !os.IsNotExist(err) {
		t.Fatalf("state file written without keys: %v", err)
	}
}

func TestSelfcheckRecordedStateMatchesNextRun(t *testing.T) {
	f := newSelfcheckFixture(t, 22)
	if code, _, stderr := f.run(t, "--record-keys", "--state", f.statePath()); code != 0 {
		t.Fatalf("record exit = %d, stderr=%s", code, stderr)
	}
	code, stdout, _ := f.run(t, "--state", f.statePath())
	if code != 0 {
		t.Fatalf("second run exit = %d", code)
	}
	if !strings.Contains(stdout, "[ OK ] keys.state") {
		t.Fatalf("keys.state should pass after recording:\n%s", stdout)
	}
}

func TestSelfcheckHelpIsUsageExit(t *testing.T) {
	f := newSelfcheckFixture(t, 22)
	code, _, _ := f.run(t, "--help")
	if code != 2 {
		t.Fatalf("exit = %d, want 2", code)
	}
}

func TestFirstNonEmpty(t *testing.T) {
	if got := firstNonEmpty("", "  ", "x", "y"); got != "x" {
		t.Fatalf("firstNonEmpty = %q, want x", got)
	}
	if got := firstNonEmpty("", "  "); got != "" {
		t.Fatalf("firstNonEmpty = %q, want empty", got)
	}
}

func TestVersionCommand(t *testing.T) {
	var stdout, stderr bytes.Buffer
	if code := runVersion(nil, &stdout, &stderr); code != 0 {
		t.Fatalf("exit = %d, stderr=%s", code, stderr.String())
	}
	lines := strings.Split(strings.TrimSpace(stdout.String()), "\n")
	if len(lines) != 3 {
		t.Fatalf("version output has %d lines:\n%s", len(lines), stdout.String())
	}
	for i, prefix := range []string{"version: ", "commit: ", "build_time: "} {
		if !strings.HasPrefix(lines[i], prefix) {
			t.Fatalf("line %d = %q, want prefix %q", i, lines[i], prefix)
		}
		if strings.TrimSpace(strings.TrimPrefix(lines[i], prefix)) == "" {
			t.Fatalf("line %d has empty value", i)
		}
	}
}

func TestVersionCommandRejectsArgs(t *testing.T) {
	var stdout, stderr bytes.Buffer
	if code := runVersion([]string{"--json"}, &stdout, &stderr); code != 2 {
		t.Fatalf("exit = %d, want 2", code)
	}
}
