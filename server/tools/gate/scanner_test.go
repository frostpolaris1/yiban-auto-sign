package main

import (
	"os"
	"path/filepath"
	"testing"
)

func writeFile(t *testing.T, root, rel, content string) {
	t.Helper()
	path := filepath.Join(root, rel)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func testRule(id, kind, owner, pattern, allow string) Rule {
	return Rule{ID: id, Kind: kind, Owner: owner, Pattern: pattern, Allow: allow, Message: id}
}

func TestScanAllowsOwner(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "internal/core/clock/clock.go", `package clock
import "time"
func Now() time.Time { return time.Now() }
`)
	rules := []Rule{testRule("clock.now", "call", "internal/core/clock/", "time.Now", "")}
	violations, err := Scan(root, rules, false)
	if err != nil {
		t.Fatalf("Scan: %v", err)
	}
	if len(violations) != 0 {
		t.Fatalf("owner must be allowed, got %#v", violations)
	}
}

func TestScanFlagsOutsideOwner(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "internal/app/foo.go", `package app
import "time"
var _ = time.Now()
`)
	rules := []Rule{testRule("clock.now", "call", "internal/core/clock/", "time.Now", "")}
	violations, err := Scan(root, rules, false)
	if err != nil {
		t.Fatalf("Scan: %v", err)
	}
	if len(violations) != 1 || violations[0].File != "internal/app/foo.go" {
		t.Fatalf("want one violation in internal/app/foo.go, got %#v", violations)
	}
}

// 文件级 owner：规则只放行 owner 指定的那一个文件。
// 同目录的第二个文件必须被拦下；目录级 owner 曾放过这一情形。
func TestScanFileOwner(t *testing.T) {
	root := t.TempDir()
	const setter = `package router
import "net/http"
func set(w http.ResponseWriter) { http.SetCookie(w, nil) }
`
	writeFile(t, root, "internal/entry/http/router/cookie.go", setter)
	writeFile(t, root, "internal/entry/http/router/sibling.go", setter)
	rules := []Rule{testRule("cookie.set", "call",
		"internal/entry/http/router/cookie.go", "http.SetCookie", "")}
	violations, err := Scan(root, rules, false)
	if err != nil {
		t.Fatalf("Scan: %v", err)
	}
	if len(violations) != 1 || violations[0].File != "internal/entry/http/router/sibling.go" {
		t.Fatalf("want only the sibling violation, got %#v", violations)
	}
}

// forbid：owner 指定的文件不得调用 pattern。owner 外不受影响。
// 与 call 的区别是适用方向相反：call 是「只有 owner 可以用」，
// forbid 是「owner 不许用，别处随意」。
func TestScanForbidFlagsUnderOwner(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "internal/adapters/pg/user.go", `package pg
import "strings"
func f(s string) string { return strings.ToLower(s) }
`)
	rules := []Rule{testRule("email.normalize.lower", "forbid",
		"internal/adapters/pg/user.go", "strings.ToLower", "")}
	violations, err := Scan(root, rules, false)
	if err != nil {
		t.Fatalf("Scan: %v", err)
	}
	if len(violations) != 1 || violations[0].File != "internal/adapters/pg/user.go" {
		t.Fatalf("want one violation in the owner file, got %#v", violations)
	}
}

func TestScanForbidSparesOutsideOwner(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "internal/adapters/pg/user.go", `package pg
func clean() {}
`)
	writeFile(t, root, "internal/app/foo.go", `package app
import "strings"
func f(s string) string { return strings.ToLower(s) }
`)
	rules := []Rule{testRule("email.normalize.lower", "forbid",
		"internal/adapters/pg/user.go", "strings.ToLower", "")}
	violations, err := Scan(root, rules, false)
	if err != nil {
		t.Fatalf("Scan: %v", err)
	}
	if len(violations) != 0 {
		t.Fatalf("forbid must not apply outside its owner, got %#v", violations)
	}
}

// 检查范围规则必须命中至少一个文件。
// owner 改名或删除时，规则会永不触发；门不得静默报绿（AGENTS §29）。
func TestValidateCheckedScopesRejectsMissingForbidOwner(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "internal/app/foo.go", "package app\n")
	rules := []Rule{testRule("email.normalize.lower", "forbid",
		"internal/adapters/pg/user.go", "strings.ToLower", "")}
	if err := ValidateCheckedScopes(root, rules); err == nil {
		t.Fatal("a forbid rule that matches no files must be rejected")
	}
}

func TestValidateCheckedScopesAcceptsExistingForbidOwner(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "internal/adapters/pg/user.go", "package pg\n")
	rules := []Rule{testRule("email.normalize.lower", "forbid",
		"internal/adapters/pg/user.go", "strings.ToLower", "")}
	if err := ValidateCheckedScopes(root, rules); err != nil {
		t.Fatalf("ValidateCheckedScopes: %v", err)
	}
}

// import 的 owner 也是检查范围，必须与 forbid 同口径 fail-closed。
func TestValidateCheckedScopesRejectsMissingImportOwner(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "internal/app/foo.go", "package app\n")
	rules := []Rule{testRule("boundary.signing", "import",
		"internal/signing/providers/", "internal/app", "")}
	if err := ValidateCheckedScopes(root, rules); err == nil {
		t.Fatal("an import rule that matches no files must be rejected")
	}
}

func TestValidateCheckedScopesAcceptsExistingImportOwner(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "internal/signing/providers/doc.go", "package providers\n")
	rules := []Rule{testRule("boundary.signing", "import",
		"internal/signing/providers/", "internal/app", "")}
	if err := ValidateCheckedScopes(root, rules); err != nil {
		t.Fatalf("ValidateCheckedScopes: %v", err)
	}
}

// allow 覆盖 owner 的全部文件时，规则实际检查零个文件。
func TestValidateCheckedScopesRejectsAllowThatCoversAll(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "internal/app/foo.go", "package app\n")
	rules := []Rule{testRule("email.normalize.lower", "forbid",
		"internal/app/", "strings.ToLower", "internal/app/")}
	if err := ValidateCheckedScopes(root, rules); err == nil {
		t.Fatal("a checked-scope rule fully exempted by allow must be rejected")
	}
}

// owner 是测试文件时，存在性检查含 _test.go；默认扫描不含。
// verify.sh 跑两遍，-tests 那一遍才真正检查该文件。
func TestValidateCheckedScopesIncludesTests(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "internal/app/user_test.go", "package app\n")
	rules := []Rule{testRule("email.normalize.lower", "forbid",
		"internal/app/user_test.go", "strings.ToLower", "")}
	if err := ValidateCheckedScopes(root, rules); err != nil {
		t.Fatalf("a test-file owner must keep the rule alive: %v", err)
	}
	if violations, err := Scan(root, rules, false); err != nil || len(violations) != 0 {
		t.Fatalf("default scan must skip test files: %#v, %v", violations, err)
	}
}

// run 必须把「owner 缺失的 forbid 配置」拦成退出码 2，而不是 ok。
func TestRunRejectsForbidWithNoTargets(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "internal/app/foo.go", "package app\n")
	rulesPath := filepath.Join(root, "rules.tsv")
	line := "email.normalize.lower\tforbid\tinternal/adapters/pg/user.go\tstrings.ToLower\t\tmsg\n"
	if err := os.WriteFile(rulesPath, []byte(line), 0o644); err != nil {
		t.Fatal(err)
	}
	if rc := run(root, rulesPath, false); rc != 2 {
		t.Fatalf("run rc = %d, want 2", rc)
	}
}

// import 与 forbid 同形状：owner 缺失必须 rc=2。
func TestRunRejectsImportWithNoTargets(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "internal/app/foo.go", "package app\n")
	rulesPath := filepath.Join(root, "rules.tsv")
	line := "boundary.signing\timport\tinternal/signing/providers/\tinternal/app\t\tmsg\n"
	if err := os.WriteFile(rulesPath, []byte(line), 0o644); err != nil {
		t.Fatal(err)
	}
	if rc := run(root, rulesPath, false); rc != 2 {
		t.Fatalf("run rc = %d, want 2", rc)
	}
}

func TestScanImportBoundary(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "internal/core/foo.go", `package core
import _ "example.com/internal/adapters/store"
`)
	rules := []Rule{testRule("boundary.core", "import", "internal/core/", "internal/adapters", "")}
	violations, err := Scan(root, rules, false)
	if err != nil {
		t.Fatalf("Scan: %v", err)
	}
	if len(violations) != 1 {
		t.Fatalf("want one import violation, got %#v", violations)
	}
}

// owner 为 * 时，规则检查每一个被扫描的文件。
// 这条规则用于「任何生产代码都不得导入某包」。
func TestScanImportAllOwners(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "internal/app/foo.go", `package app
import _ "example.com/internal/signing/fake"
`)
	writeFile(t, root, "examples/demo/other.go", `package other
import _ "example.com/internal/signing/fake"
`)
	rules := []Rule{testRule("fake.import", "import", "*", "internal/signing/fake", "")}
	violations, err := Scan(root, rules, false)
	if err != nil {
		t.Fatalf("Scan: %v", err)
	}
	if len(violations) != 2 {
		t.Fatalf("want 2 violations, got %#v", violations)
	}
}

// allow 对 import 规则同样生效：套件目录可导入，其余目录不可。
func TestScanImportAllow(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "internal/signing/conformance/suite.go", `package conformance
import _ "example.com/internal/signing/fake"
`)
	writeFile(t, root, "internal/signing/providers/yiban/provider.go", `package yiban
import _ "example.com/internal/signing/fake"
`)
	rules := []Rule{testRule("fake.import", "import", "*", "internal/signing/fake", "internal/signing/conformance/")}
	violations, err := Scan(root, rules, false)
	if err != nil {
		t.Fatalf("Scan: %v", err)
	}
	if len(violations) != 1 || violations[0].File != "internal/signing/providers/yiban/provider.go" {
		t.Fatalf("want only the provider violation, got %#v", violations)
	}
}

// 测试文件默认不在扫描口径内。
// 显式打开 -tests 后，allow 之外的测试文件同样被拦：口径差集必须可见。
func TestScanImportAllOwnersTests(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "internal/signing/providers/yiban/provider_test.go", `package yiban
import _ "example.com/internal/signing/fake"
`)
	rules := []Rule{testRule("fake.import", "import", "*", "internal/signing/fake", "internal/signing/conformance/")}
	if violations, err := Scan(root, rules, false); err != nil || len(violations) != 0 {
		t.Fatalf("tests must be skipped by default: %#v, %v", violations, err)
	}
	violations, err := Scan(root, rules, true)
	if err != nil {
		t.Fatalf("Scan: %v", err)
	}
	if len(violations) != 1 {
		t.Fatalf("with -tests want one violation, got %#v", violations)
	}
}

func TestScanFuncOwner(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "internal/app/capacity.go", `package app
func Capacity() int { return 0 }
`)
	rules := []Rule{testRule("capacity.func", "func", "internal/core/capacity/", "Capacity", "")}
	violations, err := Scan(root, rules, false)
	if err != nil {
		t.Fatalf("Scan: %v", err)
	}
	if len(violations) != 1 {
		t.Fatalf("want one func violation, got %#v", violations)
	}
}

func TestScanStringOwner(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "internal/core/status/status.go", `package status
const Success = "success"
`)
	writeFile(t, root, "internal/app/foo.go", `package app
const Success = "success"
`)
	rules := []Rule{testRule("status.literal", "string", "internal/core/status/", "pending,running,success,failed,skipped", "")}
	violations, err := Scan(root, rules, false)
	if err != nil {
		t.Fatalf("Scan: %v", err)
	}
	if len(violations) != 1 || violations[0].File != "internal/app/foo.go" {
		t.Fatalf("want only the app violation, got %#v", violations)
	}
}

func TestScanSkipsTestsByDefault(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "internal/app/foo_test.go", `package app
import "testing"
func TestX(t *testing.T) { _ = timeNowForTest() }
`)
	rules := []Rule{testRule("clock.now", "call", "internal/core/clock/", "time.Now", "")}
	if violations, err := Scan(root, rules, false); err != nil || len(violations) != 0 {
		t.Fatalf("tests must be skipped by default: %#v, %v", violations, err)
	}
}

func TestScanSkipsTestdata(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "tools/gate/testdata/bad.go", `package bad
import "time"
var _ = time.Now()
`)
	rules := []Rule{testRule("clock.now", "call", "internal/core/clock/", "time.Now", "")}
	violations, err := Scan(root, rules, false)
	if err != nil {
		t.Fatalf("Scan: %v", err)
	}
	if len(violations) != 0 {
		t.Fatalf("testdata must be skipped, got %#v", violations)
	}
}

// 非 import 规则的 allow 路径：allow 内的文件豁免，其余照常命中。
// 该路径在 ruleApplies 重构后改走 underAny，必须单独钉住。
func TestScanNonImportAllow(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "internal/testsupport/clock.go", `package testsupport
import "time"
var _ = time.Now()
`)
	writeFile(t, root, "internal/app/foo.go", `package app
import "time"
var _ = time.Now()
`)
	rules := []Rule{testRule("clock.now", "call", "internal/core/clock/", "time.Now", "internal/testsupport/")}
	violations, err := Scan(root, rules, false)
	if err != nil {
		t.Fatalf("Scan: %v", err)
	}
	if len(violations) != 1 || violations[0].File != "internal/app/foo.go" {
		t.Fatalf("want only the app violation, got %#v", violations)
	}
}

// 加载时拒绝语义反转写法：非 import 规则的 owner *，以及任何规则的 allow *。
// 两者都会让规则永不生效，且门会静默报绿。
func TestLoadRulesRejectsWildcardInversion(t *testing.T) {
	cases := []struct {
		name string
		line string
	}{
		{"non-import owner", "clock.now\tcall\t*\ttime.Now\t\tmsg"},
		{"non-import allow", "clock.now\tcall\tinternal/core/clock/\ttime.Now\t*\tmsg"},
		{"import allow", "fake.import\timport\t*\tinternal/signing/fake\t*\tmsg"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "rules.tsv")
			if err := os.WriteFile(path, []byte(tc.line+"\n"), 0o644); err != nil {
				t.Fatal(err)
			}
			if _, err := LoadRules(path); err == nil {
				t.Fatalf("LoadRules accepted %q", tc.line)
			}
		})
	}
}

// import 规则的 owner * 是合法写法：它表示检查全部被扫描文件。
func TestLoadRulesAllowsImportOwnerWildcard(t *testing.T) {
	path := filepath.Join(t.TempDir(), "rules.tsv")
	line := "fake.import\timport\t*\tinternal/signing/fake\tinternal/signing/conformance/\tmsg\n"
	if err := os.WriteFile(path, []byte(line), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadRules(path); err != nil {
		t.Fatalf("LoadRules: %v", err)
	}
}

// forbid 的 owner 为空等于检查零个文件：加载时必须拒绝。
// 接受它就是一条永不触发的规则，门会静默报绿（AGENTS §29）。
func TestLoadRulesRejectsEmptyForbidOwner(t *testing.T) {
	path := filepath.Join(t.TempDir(), "rules.tsv")
	line := "email.normalize.lower\tforbid\t\tstrings.ToLower\t\tmsg\n"
	if err := os.WriteFile(path, []byte(line), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadRules(path); err == nil {
		t.Fatal("LoadRules accepted a forbid rule that checks no files")
	}
}

func TestLoadRulesEmptyIsError(t *testing.T) {
	path := filepath.Join(t.TempDir(), "empty.tsv")
	if err := os.WriteFile(path, []byte("# only a comment\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadRules(path); err == nil {
		t.Fatal("an empty rule set must be an error")
	}
}

func TestLoadRealCanonical(t *testing.T) {
	rules, err := LoadRules(filepath.Join("..", "..", "canonical.tsv"))
	if err != nil {
		t.Fatalf("LoadRules: %v", err)
	}
	if len(rules) < 20 {
		t.Fatalf("canonical.tsv has %d rules, want at least 20", len(rules))
	}
}

func TestCountGoFilesEmptyTree(t *testing.T) {
	count, err := CountGoFiles(t.TempDir(), false)
	if err != nil {
		t.Fatal(err)
	}
	if count != 0 {
		t.Fatalf("count = %d, want 0", count)
	}
}

func TestCountGoFilesSkipsCaches(t *testing.T) {
	root := t.TempDir()
	for _, dir := range []string{"a", "vendor", "node_modules", "testdata"} {
		if err := os.MkdirAll(filepath.Join(root, dir), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(root, "a", "x.go"), []byte("package a\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	for _, dir := range []string{"vendor", "node_modules", "testdata"} {
		if err := os.WriteFile(filepath.Join(root, dir, "y.go"), []byte("package y\n"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	count, err := CountGoFiles(root, false)
	if err != nil {
		t.Fatal(err)
	}
	if count != 1 {
		t.Fatalf("count = %d, want 1", count)
	}
}

// 只有测试文件的树，在不含测试时不算被扫描的树。
func TestCountGoFilesRespectsIncludeTests(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "x_test.go"), []byte("package x\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if count, err := CountGoFiles(root, false); err != nil || count != 0 {
		t.Fatalf("without tests count=%d err=%v, want 0", count, err)
	}
	if count, err := CountGoFiles(root, true); err != nil || count != 1 {
		t.Fatalf("with tests count=%d err=%v, want 1", count, err)
	}
}

// 拒绝路径就是本门的边界：
// 没有真正扫描的树**绝不可报告 ok**。
func TestRunRefusesEmptyScans(t *testing.T) {
	// 用最小规则集：本用例只测「扫描为空」与 -tests 口径。
	// 真实 canonical.tsv 含 checked-scope 规则；空树会先被 CountGoFiles
	// 以 no Go files 拦下，owner 缺失另由 TestRunRejects*WithNoTargets 覆盖。
	rules := filepath.Join(t.TempDir(), "rules.tsv")
	if err := os.WriteFile(rules, []byte("clock.now\tcall\tinternal/core/clock/\ttime.Now\t\tuse clock.Clock.Now\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	empty := t.TempDir()
	if code := run(empty, rules, false); code != 2 {
		t.Fatalf("empty tree: code = %d, want 2", code)
	}

	tests := t.TempDir()
	if err := os.WriteFile(filepath.Join(tests, "x_test.go"), []byte("package x\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if code := run(tests, rules, false); code != 2 {
		t.Fatalf("test-only tree without -tests: code = %d, want 2", code)
	}
	if code := run(tests, rules, true); code != 0 {
		t.Fatalf("test-only tree with -tests: code = %d, want 0", code)
	}
}

func TestRunReportsViolations(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "internal/core/clock/x.go", "package clock\n\nimport \"time\"\n\nvar _ = time.Now()\n")
	rules := filepath.Join(t.TempDir(), "canonical.tsv")
	writeFile(t, "", rules, "clock.now\tcall\tinternal/core/clock/\ttime.Now\tinternal/core/clock/\tuse clock.Clock.Now\n")
	if code := run(root, rules, false); code != 0 {
		t.Fatalf("inside owner: code = %d, want 0", code)
	}

	writeFile(t, root, "internal/app/x.go", "package app\n\nimport \"time\"\n\nvar _ = time.Now()\n")
	if code := run(root, rules, false); code != 1 {
		t.Fatalf("outside owner: code = %d, want 1", code)
	}
}
