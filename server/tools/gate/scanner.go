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

// Rule 是一条规范归属规则。
type Rule struct {
	ID      string
	Kind    string // call | string | func | type | import
	Owner   string
	Pattern string
	Allow   string
	Message string
}

// Violation 是一次规则命中。
type Violation struct {
	File string
	Line int
	Rule Rule
}

// LoadRules 读取制表符分隔的规范文件。
// **空规则集是错误**：空扫描绝不可显得绿。
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

// walkGoFiles 遍历扫描会考虑的全部文件。
// Scan 与 CountGoFiles 都用它，故两者看到的集合不会漂移。
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

// Scan 遍历 root，返回全部规则命中。
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
		// import 规则：owner 内的文件不得导入 pattern 的东西。
		// allow 同样生效；owner 为 * 时检查全部被扫描的文件。
		return under(rel, r.Owner) && !underAny(rel, r.Allow)
	}
	if under(rel, r.Owner) {
		return false
	}
	return !underAny(rel, r.Allow)
}

// underAny 判断 rel 是否落在任一允许前缀之下。
func underAny(rel, list string) bool {
	for _, prefix := range strings.Split(list, ",") {
		if under(rel, prefix) {
			return true
		}
	}
	return false
}

// under 判断 rel 是否落在 prefix 之下。
//
// prefix 以 .go 结尾时按**单文件精确匹配**：
// 需要把概念钉在一个文件、而不是整个目录时用这种写法。
// prefix 为 * 时匹配全部文件：用于没有单一属主的规则，
// 例如「任何生产代码都不得导入这个包」。
// 其余 prefix 按目录前缀比较。
func under(rel, prefix string) bool {
	prefix = strings.TrimSpace(prefix)
	if prefix == "" {
		return false
	}
	if prefix == "*" {
		return true
	}
	if strings.HasSuffix(prefix, ".go") {
		return rel == prefix
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
				return true // 结构体标签与原始串不是状态值
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

// CountGoFiles 返回同一 includeTests 口径下扫描会考虑的文件数。
// 调用方用它拒绝一次什么都没扫的运行。
func CountGoFiles(root string, includeTests bool) (int, error) {
	count := 0
	err := walkGoFiles(root, includeTests, func(string) error {
		count++
		return nil
	})
	return count, err
}
