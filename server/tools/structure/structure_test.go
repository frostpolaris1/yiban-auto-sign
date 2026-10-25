package main

import (
	"os"
	"path/filepath"
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
	writeFile(t, path, "ui/srcx\ta\tnote\n")
	if _, err := loadRules(path); err == nil {
		t.Fatal("unknown scope must fail")
	}
}

func TestLoadRulesParsesScopes(t *testing.T) {
	path := filepath.Join(t.TempDir(), "structure.tsv")
	writeFile(t, path, "root\tserver,ui\tnote\nui/src\tapp,pages\tnote\n")
	rules, err := loadRules(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(rules) != 2 || !rules[1].allow["pages"] {
		t.Fatalf("unexpected rules: %#v", rules)
	}
}

func TestCheckAllowlistFlagsUnregistered(t *testing.T) {
	root := t.TempDir()
	for _, dir := range []string{"server/core", "server/stray"} {
		if err := os.MkdirAll(filepath.Join(root, dir), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	rules := []rule{{scope: "server", allow: map[string]bool{"core": true}}}
	problems, seen, err := checkAllowlist(root, rules)
	if err != nil {
		t.Fatal(err)
	}
	if seen != 1 {
		t.Fatalf("seen = %d", seen)
	}
	if len(problems) != 1 || !strings.Contains(problems[0], "stray") {
		t.Fatalf("problems = %#v", problems)
	}
}

func TestCheckAllowlistAcceptsRegistered(t *testing.T) {
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, "server", "core"), 0o755); err != nil {
		t.Fatal(err)
	}
	rules := []rule{{scope: "server", allow: map[string]bool{"core": true}}}
	problems, seen, err := checkAllowlist(root, rules)
	if err != nil {
		t.Fatal(err)
	}
	if seen != 1 || len(problems) != 0 {
		t.Fatalf("seen=%d problems=%#v", seen, problems)
	}
}

func TestCheckFrontendRules(t *testing.T) {
	root := t.TempDir()
	src := filepath.Join(root, "ui", "src")
	writeFile(t, filepath.Join(src, "pages", "Dashboard.tsx"), "export const Dashboard = 1\n")
	writeFile(t, filepath.Join(src, "components", "ui", "Bad.tsx"), "import x from '@/features/auth/x'\nexport default Bad\n")
	writeFile(t, filepath.Join(src, "features", "auth", "a.ts"), "import '@/features/school/b'\n")
	writeFile(t, filepath.Join(src, "features", "school", "b.ts"), "export const b = 1\n")
	writeFile(t, filepath.Join(src, "lib", "ok.ts"), "export const ok = 1\n")

	problems, err := checkFrontend(root)
	if err != nil {
		t.Fatal(err)
	}
	joined := strings.Join(problems, "\n")
	for _, want := range []string{
		"page files must be named",
		"use named exports",
		"components must not import features",
		"feature must not import feature",
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
	writeFile(t, filepath.Join(src, "pages", "DashboardPage.tsx"), "import '@/features/auth/LoginForm'\nexport const DashboardPage = 1\n")
	writeFile(t, filepath.Join(src, "pages", "LoginPage.tsx"), "import '@/features/dashboard/Card'\nexport const LoginPage = 1\n")
	writeFile(t, filepath.Join(src, "features", "auth", "LoginForm.tsx"), "import '@/features/auth/helpers'\nexport const LoginForm = 1\n")
	writeFile(t, filepath.Join(src, "features", "auth", "helpers.ts"), "export const helpers = 1\n")
	writeFile(t, filepath.Join(src, "features", "dashboard", "Card.tsx"), "export const Card = 1\n")
	writeFile(t, filepath.Join(src, "components", "ui", "Button.tsx"), "export const Button = 1\n")

	problems, err := checkFrontend(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(problems) != 0 {
		t.Fatalf("problems = %#v", problems)
	}
}
