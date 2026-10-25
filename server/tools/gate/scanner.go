package main

import (
	"bufio"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// Rule is one canonical-owner rule.
type Rule struct {
	ID      string
	Kind    string // call | string | func | type | import
	Owner   string
	Pattern string
	Allow   string
	Message string
}

// Violation is one rule hit.
type Violation struct {
	File string
	Line int
	Rule Rule
}

// LoadRules reads the tab-separated canonical file.
// An empty rule set is an error: an empty scan must never look green.
func LoadRules(path string) ([]Rule, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	var rules []Rule
	sc := bufio.NewScanner(f)
	lineNo := 0
	for sc.Scan() {
		lineNo++
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		parts := strings.Split(line, "\t")
		if len(parts) < 6 {
			return nil, fmt.Errorf("%s:%d: want 6 tab-separated columns, got %d", path, lineNo, len(parts))
		}
		r := Rule{
			ID:      parts[0],
			Kind:    parts[1],
			Owner:   parts[2],
			Pattern: parts[3],
			Allow:   parts[4],
			Message: parts[5],
		}
		if r.ID == "" || r.Kind == "" || r.Pattern == "" {
			return nil, fmt.Errorf("%s:%d: empty id, kind or pattern", path, lineNo)
		}
		switch r.Kind {
		case "call", "string", "func", "type", "import":
		default:
			return nil, fmt.Errorf("%s:%d: unknown kind %q", path, lineNo, r.Kind)
		}
		rules = append(rules, r)
	}
	if err := sc.Err(); err != nil {
		return nil, err
	}
	if len(rules) == 0 {
		return nil, fmt.Errorf("%s: rules file is empty", path)
	}
	return rules, nil
}

// walkGoFiles visits every file a scan considers. Scan and CountGoFiles both
// use it, so the set they see cannot drift.
func walkGoFiles(root string, includeTests bool, fn func(path string) error) error {
	return filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			switch d.Name() {
			case ".git", "vendor", "node_modules", "testdata":
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, ".go") {
			return nil
		}
		if !includeTests && strings.HasSuffix(path, "_test.go") {
			return nil
		}
		return fn(path)
	})
}

// Scan walks root and returns every rule hit.
func Scan(root string, rules []Rule, includeTests bool) ([]Violation, error) {
	var out []Violation
	err := walkGoFiles(root, includeTests, func(path string) error {
		rel, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		rel = filepath.ToSlash(rel)
		fset := token.NewFileSet()
		file, err := parser.ParseFile(fset, path, nil, 0)
		if err != nil {
			return fmt.Errorf("%s: %w", rel, err)
		}
		for _, rule := range rules {
			if !ruleApplies(rule, rel) {
				continue
			}
			out = append(out, inspectFile(file, fset, rel, rule)...)
		}
		return nil
	})
	return out, err
}

func ruleApplies(r Rule, rel string) bool {
	if r.Kind == "import" {
		return under(rel, r.Owner)
	}
	if under(rel, r.Owner) {
		return false
	}
	for _, allow := range strings.Split(r.Allow, ",") {
		if under(rel, allow) {
			return false
		}
	}
	return true
}

func under(rel, prefix string) bool {
	prefix = strings.TrimSpace(prefix)
	if prefix == "" {
		return false
	}
	prefix = strings.TrimSuffix(prefix, "/") + "/"
	return strings.HasPrefix(rel, prefix)
}

func inspectFile(file *ast.File, fset *token.FileSet, rel string, r Rule) []Violation {
	var out []Violation
	add := func(n ast.Node) {
		out = append(out, Violation{File: rel, Line: fset.Position(n.Pos()).Line, Rule: r})
	}
	ast.Inspect(file, func(n ast.Node) bool {
		switch r.Kind {
		case "call":
			call, ok := n.(*ast.CallExpr)
			if !ok {
				return true
			}
			if name, ok := selectorName(call.Fun); ok && name == r.Pattern {
				add(call)
			}
		case "func":
			fn, ok := n.(*ast.FuncDecl)
			if ok && fn.Name != nil && fn.Name.Name == r.Pattern {
				add(fn)
			}
		case "type":
			ts, ok := n.(*ast.TypeSpec)
			if ok && ts.Name != nil && ts.Name.Name == r.Pattern {
				add(ts)
			}
		case "string":
			lit, ok := n.(*ast.BasicLit)
			if !ok || lit.Kind != token.STRING {
				return true
			}
			if strings.HasPrefix(lit.Value, "`") {
				return true // struct tags and raw strings are not status values
			}
			value, err := strconv.Unquote(lit.Value)
			if err != nil {
				return true
			}
			for _, pattern := range strings.Split(r.Pattern, ",") {
				if value == pattern {
					add(lit)
					break
				}
			}
		case "import":
			spec, ok := n.(*ast.ImportSpec)
			if !ok {
				return true
			}
			path, err := strconv.Unquote(spec.Path.Value)
			if err != nil {
				return true
			}
			for _, pattern := range strings.Split(r.Pattern, ",") {
				if strings.Contains(path, pattern) {
					add(spec)
					break
				}
			}
		}
		return true
	})
	return out
}

func selectorName(e ast.Expr) (string, bool) {
	sel, ok := e.(*ast.SelectorExpr)
	if !ok {
		return "", false
	}
	id, ok := sel.X.(*ast.Ident)
	if !ok {
		return "", false
	}
	return id.Name + "." + sel.Sel.Name, true
}

// CountGoFiles returns how many Go files a scan with the same includeTests
// flag would consider. A caller uses it to reject a run that scans nothing.
func CountGoFiles(root string, includeTests bool) (int, error) {
	count := 0
	err := walkGoFiles(root, includeTests, func(string) error {
		count++
		return nil
	})
	return count, err
}
