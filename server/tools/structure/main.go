// Command structure enforces the repository layout.
//
// It reads structure.tsv, checks that every immediate child directory of each
// managed scope is registered, and applies the front-end naming and import
// rules. An empty rules file, an empty allowlist or an unknown scope is an
// error: the gate must never pass by scanning nothing.
package main

import (
	"bufio"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

// rule is one managed directory and its registered child directories.
type rule struct {
	scope string
	allow map[string]bool
}

var (
	pageName    = regexp.MustCompile(`^[A-Z][A-Za-z0-9]*Page\.tsx$`)
	lowerName   = regexp.MustCompile(`^[a-z][a-z0-9]*$`)
	featurePath = regexp.MustCompile(`@/features/([a-zA-Z0-9_-]+)`)
	skipDirs    = map[string]bool{".git": true, "node_modules": true}
)

func main() {
	root := flag.String("root", "..", "repository root")
	rulesPath := flag.String("rules", "structure.tsv", "rules file")
	flag.Parse()

	rules, err := loadRules(*rulesPath)
	if err != nil {
		fatal(err)
	}
	problems, seen, err := checkAllowlist(*root, rules)
	if err != nil {
		fatal(err)
	}
	if seen == 0 {
		fatal(fmt.Errorf("structure: no managed directory exists; refused to pass"))
	}
	uiProblems, err := checkFrontend(*root)
	if err != nil {
		fatal(err)
	}
	problems = append(problems, uiProblems...)
	if len(problems) > 0 {
		sort.Strings(problems)
		for _, p := range problems {
			fmt.Fprintln(os.Stderr, p)
		}
		fatal(fmt.Errorf("structure: %d problem(s)", len(problems)))
	}
	fmt.Printf("structure: ok (%d scopes, %d rules)\n", seen, len(rules))
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, err)
	os.Exit(1)
}

// loadRules parses scope<TAB>allow,allow<TAB>note lines.
func loadRules(path string) ([]rule, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	var rules []rule
	sc := bufio.NewScanner(f)
	for line := 1; sc.Scan(); line++ {
		text := strings.TrimSpace(sc.Text())
		if text == "" || strings.HasPrefix(text, "#") {
			continue
		}
		parts := strings.Split(sc.Text(), "\t")
		if len(parts) < 2 {
			return nil, fmt.Errorf("%s:%d: want scope, allow and note", path, line)
		}
		scope := strings.TrimSpace(parts[0])
		if !knownScope(scope) {
			return nil, fmt.Errorf("%s:%d: unknown scope %q", path, line, scope)
		}
		allow := map[string]bool{}
		for _, name := range strings.Split(parts[1], ",") {
			name = strings.TrimSpace(name)
			if name == "" {
				continue
			}
			allow[name] = true
		}
		if len(allow) == 0 {
			return nil, fmt.Errorf("%s:%d: empty allowlist", path, line)
		}
		rules = append(rules, rule{scope: scope, allow: allow})
	}
	if err := sc.Err(); err != nil {
		return nil, err
	}
	if len(rules) == 0 {
		return nil, fmt.Errorf("%s: no rules; refused to pass", path)
	}
	return rules, nil
}

func knownScope(scope string) bool {
	if scope == "root" {
		return true
	}
	parts := strings.Split(scope, "/")
	if len(parts) < 2 {
		return false
	}
	switch parts[0] {
	case "server":
		switch parts[1] {
		case "internal", "migrations", "cmd", "contracts", "tools", "scripts":
			return true
		}
	case "ui":
		return parts[1] == "src"
	}
	return false
}

// checkAllowlist reports unregistered child directories and counts the scopes
// that exist on disk.
func checkAllowlist(root string, rules []rule) ([]string, int, error) {
	var problems []string
	seen := 0
	for _, r := range rules {
		dir := root
		if r.scope != "root" {
			dir = filepath.Join(root, filepath.FromSlash(r.scope))
		}
		entries, err := os.ReadDir(dir)
		if err != nil {
			if os.IsNotExist(err) {
				fmt.Printf("structure: skip missing scope %s\n", r.scope)
				continue
			}
			return nil, 0, err
		}
		seen++
		for _, e := range entries {
			if !e.IsDir() || skipDirs[e.Name()] {
				continue
			}
			if !r.allow[e.Name()] {
				problems = append(problems, fmt.Sprintf("%s: unregistered directory %q (register it in structure.tsv)", r.scope, e.Name()))
			}
		}
	}
	return problems, seen, nil
}

// checkFrontend applies page naming, feature naming and import direction.
func checkFrontend(root string) ([]string, error) {
	src := filepath.Join(root, "ui", "src")
	if _, err := os.Stat(src); os.IsNotExist(err) {
		return nil, nil
	} else if err != nil {
		return nil, err
	}

	var problems []string
	err := filepath.WalkDir(src, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			if skipDirs[d.Name()] {
				return filepath.SkipDir
			}
			name := d.Name()
			rel, _ := filepath.Rel(src, path)
			rel = filepath.ToSlash(rel)
			if (strings.HasPrefix(rel, "features/") || strings.HasPrefix(rel, "components/")) && !lowerName.MatchString(name) {
				problems = append(problems, fmt.Sprintf("ui/src/%s: directory name must be lowercase ascii", rel))
			}
			return nil
		}

		name := d.Name()
		if !strings.HasSuffix(name, ".ts") && !strings.HasSuffix(name, ".tsx") {
			return nil
		}
		rel, _ := filepath.Rel(src, path)
		rel = filepath.ToSlash(rel)

		if strings.HasPrefix(rel, "pages/") && strings.HasSuffix(name, ".tsx") && !pageName.MatchString(name) {
			problems = append(problems, fmt.Sprintf("ui/src/%s: page files must be named <Name>Page.tsx", rel))
		}

		body, readErr := os.ReadFile(path)
		if readErr != nil {
			return readErr
		}
		text := string(body)
		if strings.Contains(text, "export default") {
			problems = append(problems, fmt.Sprintf("ui/src/%s: use named exports, not export default", rel))
		}
		if strings.HasPrefix(rel, "components/") && strings.Contains(text, "@/features") {
			problems = append(problems, fmt.Sprintf("ui/src/%s: components must not import features", rel))
		}
		if strings.HasPrefix(rel, "components/") && strings.Contains(text, "@/pages") {
			problems = append(problems, fmt.Sprintf("ui/src/%s: components must not import pages", rel))
		}
		if strings.HasPrefix(rel, "features/") {
			if strings.Contains(text, "@/pages") {
				problems = append(problems, fmt.Sprintf("ui/src/%s: features must not import pages", rel))
			}
			if m := featurePath.FindStringSubmatch(text); m != nil {
				rest := strings.TrimPrefix(rel, "features/")
				if !strings.HasPrefix(rest, m[1]+"/") {
					problems = append(problems, fmt.Sprintf("ui/src/%s: feature must not import feature %q", rel, m[1]))
				}
			}
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return problems, nil
}
