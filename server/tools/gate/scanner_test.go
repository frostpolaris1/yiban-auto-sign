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
