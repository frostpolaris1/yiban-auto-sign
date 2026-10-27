package main

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

func writeFile(t *testing.T, path, body string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func mkdirs(t *testing.T, root string, dirs ...string) {
	t.Helper()
	for _, dir := range dirs {
		if err := os.MkdirAll(filepath.Join(root, dir), 0o755); err != nil {
			t.Fatal(err)
		}
	}
}

func TestLoadRulesRejectsEmptyFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "structure.tsv")
	writeFile(t, path, "# only a comment\n")
	if _, err := loadRules(path); err == nil {
		t.Fatal("empty rules must fail")
	}
}

func TestLoadRulesRejectsEmptyAllowlist(t *testing.T) {
	path := filepath.Join(t.TempDir(), "structure.tsv")
	writeFile(t, path, "ui/src\t\tnote\n")
	if _, err := loadRules(path); err == nil {
		t.Fatal("empty allowlist must fail")
	}
}

func TestLoadRulesRejectsUnknownScope(t *testing.T) {
	path := filepath.Join(t.TempDir(), "structure.tsv")
	for _, scope := range []string{"ui/srcx", "server/internals", "server"} {
		writeFile(t, path, scope+"\ta\tnote\n")
		if _, err := loadRules(path); err == nil {
			t.Fatalf("scope %q must fail", scope)
		}
	}
}

func TestLoadRulesParsesOptionalScope(t *testing.T) {
	path := filepath.Join(t.TempDir(), "structure.tsv")
	writeFile(t, path, "root\tserver,ui\tnote\n?server/internal/app\tauth\tnote\n")
	rules, err := loadRules(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(rules) != 2 || !rules[1].optional || rules[1].scope != "server/internal/app" {
		t.Fatalf("unexpected rules: %#v", rules)
	}
}

func TestCheckAllowlistFlagsUnregistered(t *testing.T) {
	root := t.TempDir()
	mkdirs(t, root, "server/core", "server/stray")
	rules := []rule{{scope: "server", allow: map[string]bool{"core": true}}}
	problems, seen, err := checkAllowlist(root, rules)
	if err != nil {
		t.Fatal(err)
	}
	if seen != 1 || len(problems) != 1 || !strings.Contains(problems[0], "stray") {
		t.Fatalf("seen=%d problems=%#v", seen, problems)
	}
}

func TestCheckAllowlistAcceptsRegistered(t *testing.T) {
	root := t.TempDir()
	mkdirs(t, root, "server/core")
	rules := []rule{{scope: "server", allow: map[string]bool{"core": true}}}
	problems, seen, err := checkAllowlist(root, rules)
	if err != nil {
		t.Fatal(err)
	}
	if seen != 1 || len(problems) != 0 {
		t.Fatalf("seen=%d problems=%#v", seen, problems)
	}
}

func TestCheckAllowlistMissingScope(t *testing.T) {
	root := t.TempDir()
	mkdirs(t, root, "server/core")
	rules := []rule{
		{scope: "server", allow: map[string]bool{"core": true}},
		{scope: "server/internal", allow: map[string]bool{"app": true}},
		{scope: "ui/src", optional: true, allow: map[string]bool{"app": true}},
	}
	problems, seen, err := checkAllowlist(root, rules)
	if err != nil {
		t.Fatal(err)
	}
	if seen != 1 {
		t.Fatalf("seen = %d, want 1", seen)
	}
	if len(problems) != 1 || !strings.Contains(problems[0], "server/internal: scope is missing") {
		t.Fatalf("problems = %#v", problems)
	}
}

func TestCheckAllowlistSkipsDotDirsAtRoot(t *testing.T) {
	root := t.TempDir()
	mkdirs(t, root, ".github", ".beads", "server")
	rules := []rule{{scope: "root", allow: map[string]bool{"server": true}}}
	problems, _, err := checkAllowlist(root, rules)
	if err != nil {
		t.Fatal(err)
	}
	if len(problems) != 0 {
		t.Fatalf("problems = %#v", problems)
	}
}

func TestCheckFrontendRules(t *testing.T) {
	root := t.TempDir()
	src := filepath.Join(root, "ui", "src")
	writeFile(t, filepath.Join(src, "pages", "Dashboard.tsx"), "export const Dashboard = 1\n")
	writeFile(t, filepath.Join(src, "components", "ui", "Bad.tsx"), "export default Bad\n")
	writeFile(t, filepath.Join(src, "features", "Auth", "a.ts"), "export const a = 1\n")
	writeFile(t, filepath.Join(src, "lib", "ok.ts"), "export const ok = 1\n")

	problems, err := checkFrontend(root)
	if err != nil {
		t.Fatal(err)
	}
	joined := strings.Join(problems, "\n")
	for _, want := range []string{
		"page files must be named",
		"use named exports",
		"directory name must be lowercase",
	} {
		if !strings.Contains(joined, want) {
			t.Fatalf("missing %q in:\n%s", want, joined)
		}
	}
	if strings.Contains(joined, "lib/ok.ts") {
		t.Fatalf("clean file reported:\n%s", joined)
	}
}

func TestCheckFrontendAcceptsCleanTree(t *testing.T) {
	root := t.TempDir()
	src := filepath.Join(root, "ui", "src")
	writeFile(t, filepath.Join(src, "pages", "DashboardPage.tsx"), "export const DashboardPage = 1\n")
	writeFile(t, filepath.Join(src, "features", "auth", "LoginForm.tsx"), "export const LoginForm = 1\n")
	writeFile(t, filepath.Join(src, "components", "ui", "Button.tsx"), "export const Button = 1\n")

	problems, err := checkFrontend(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(problems) != 0 {
		t.Fatalf("problems = %#v", problems)
	}
}

// 已存在但空的前端目录**不得通过**。
func TestCheckFrontendEmptyScanFails(t *testing.T) {
	root := t.TempDir()
	mkdirs(t, root, "ui/src/app")
	problems, err := checkFrontend(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(problems) != 1 || !strings.Contains(problems[0], "no TypeScript files") {
		t.Fatalf("problems = %#v", problems)
	}
}

// 受管 scope 清单是钉子：删 scope 或改 scope 名必须同步改这里。
// 删一条 scope 等于把该目录从结构门里摘掉（其下新目录全放行），
// 必须是一次显式决定，而不是一次静默的绿（AGENTS §29）。
func TestStructureScopesPinned(t *testing.T) {
	want := []string{
		"root",
		"server/internal",
		"server/cmd",
		"server/migrations",
		"server/internal/core",
		"server/internal/app",
		"server/internal/adapters",
		"server/internal/entry/http",
		"ui/src",
		"ui/src/features",
		"ui/src/components",
	}
	rules, err := loadRules(filepath.Join("..", "..", "structure.tsv"))
	if err != nil {
		t.Fatalf("load structure: %v", err)
	}
	if len(rules) != len(want) {
		t.Fatalf("structure has %d scopes, pinned list has %d; add or remove must update this list",
			len(rules), len(want))
	}
	for i, r := range rules {
		if r.scope != want[i] {
			t.Fatalf("scope %d = %q, pinned %q; rename must be an explicit decision",
				i, r.scope, want[i])
		}
	}
}

// 结构表整行快照：scope、allowlist 与 optional 的改动都要显式决定。
// 只钉 scope 序列会让 allowlist 的删项静默通过（r4c 复审 P2）。
func TestStructureRowsPinned(t *testing.T) {
	rules, err := loadRules(filepath.Join("..", "..", "structure.tsv"))
	if err != nil {
		t.Fatalf("load structure: %v", err)
	}
	h := sha256.New()
	for _, r := range rules {
		keys := make([]string, 0, len(r.allow))
		for k := range r.allow {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		fmt.Fprintf(h, "%s\t%s\t%t\t%s\n", r.scope, strings.Join(keys, ","), r.optional, r.note)
	}
	got := hex.EncodeToString(h.Sum(nil))
	const want = "239d62233303d7080a10c76c99e81d219a8d5c619081c39125dce5946cc57f1f"
	if got != want {
		t.Fatalf("structure 内容变了（hash %s）；确认改动后更新 TestStructureRowsPinned 的哈希", got)
	}
}
