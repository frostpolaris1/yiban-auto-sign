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
	Kind    string // call | string | func | type | import | forbid
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
	seen := make(map[string]bool)
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
		// 重复 ID 会让锚点检查串味：后一条的落点会替前一条作证。
		if seen[r.ID] {
			return nil, fmt.Errorf("%s:%d: duplicate rule id %q", path, lineNo, r.ID)
		}
		seen[r.ID] = true
		switch r.Kind {
		case "call", "string", "func", "type", "import", "forbid":
		default:
			return nil, fmt.Errorf("%s:%d: unknown kind %q", path, lineNo, r.Kind)
		}
		// under 把 prefix == "*" 解释为「匹配全部文件」。
		// import 与 forbid 的 owner 是**检查范围**：* 表示全查，语义成立。
		// 其余规则的 owner 是**唯一属主**（命中即豁免）：owner 为 * 会让
		// call/string/func/type 规则永不生效，load 时拒绝。
		if r.Kind != "import" && r.Kind != "forbid" && strings.TrimSpace(r.Owner) == "*" {
			return nil, fmt.Errorf("%s:%d: rule %q must use a concrete owner (owner * is only for import and forbid rules)",
				path, lineNo, r.ID)
		}
		// forbid 的 owner 为空等于检查零个文件。接受它就是一条永不触发的规则。
		if r.Kind == "forbid" && strings.TrimSpace(r.Owner) == "" {
			return nil, fmt.Errorf("%s:%d: forbid rule %q needs a non-empty owner; it would check no files",
				path, lineNo, r.ID)
		}
		for _, prefix := range strings.Split(r.Allow, ",") {
			if strings.TrimSpace(prefix) == "*" {
				return nil, fmt.Errorf("%s:%d: rule %q must not use allow *",
					path, lineNo, r.ID)
			}
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
	// import 与 forbid 的 owner 是**检查范围**：只查 owner 之下的文件。
	// 其余规则的 owner 是**唯一属主**：owner 之下的文件被放行，其余被检查。
	// 两种语义方向相反，混用会让门静默失效，故在注释与测试里分开写。
	if r.Kind == "import" || r.Kind == "forbid" {
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
// prefix 为 * 时匹配全部文件：用于没有单一属主的 import 规则，
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
		case "call", "forbid":
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

// ValidateCheckedScopes 要求每条 import 与 forbid 规则在 root 下至少命中一个 Go 文件。
//
// import 与 forbid 的 owner 都是**检查范围**：import 查范围内的导入，
// forbid 查范围内的调用。owner 改名或删除后，规则命中零个文件，永不触发；
// 门会打印 ok，静默失守（AGENTS §29）。本检查把这种配置变成 rc=2。
// 判据是「owner 路径是否存在」，故固定按含 _test.go 的口径统计，
// 与本轮 Scan 的 includeTests 无关。测试文件作为 owner 时，
// 默认扫描看不到它，只有 verify.sh 的 -tests 那一遍会真正检查。
func ValidateCheckedScopes(root string, rules []Rule) error {
	var rels []string
	if err := walkGoFiles(root, true, func(path string) error {
		rel, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		rels = append(rels, filepath.ToSlash(rel))
		return nil
	}); err != nil {
		return err
	}
	for _, r := range rules {
		if r.Kind != "import" && r.Kind != "forbid" {
			continue
		}
		hit := false
		for _, rel := range rels {
			if ruleApplies(r, rel) {
				hit = true
				break
			}
		}
		if !hit {
			return fmt.Errorf("%s rule %q checks no files under %q; refused to pass", r.Kind, r.ID, r.Owner)
		}
	}
	return nil
}

// ValidateRuleAnchors 要求每条非 import/forbid 规则的 owner 与 pattern 都还有落点。
//
// 这几类规则（call/func/type/string）的 owner 是**唯一属主**：owner 下的文件放行，
// 其余文件被检查。owner 目录改名或 pattern 符号改名后，规则仍会「通过」，
// 但它实际什么都没检查（AGENTS §29：静默的不重合比红更危险）。
// 本检查把两种空转变成 rc=2：
//   - owner 在 root 下匹配零个文件；
//   - pattern 在整棵树里一次都没出现（call 看选择子调用，func/type 看声明名，
//     string 看字符串字面量）。
//
// string 的 pattern 可能是逗号分隔的多个值；只要其中一个仍存在即视为有落点。
// 个别值被删除是正常演进，整组改名才是要拦的形状。
func ValidateRuleAnchors(root string, rules []Rule, includeTests bool) error {
	found := make(map[string]bool)
	owners := make(map[string]bool)
	err := walkGoFiles(root, includeTests, func(path string) error {
		rel, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		rel = filepath.ToSlash(rel)
		for _, r := range rules {
			if r.Kind == "import" || r.Kind == "forbid" {
				continue
			}
			if under(rel, r.Owner) {
				owners[r.ID] = true
			}
		}
		fset := token.NewFileSet()
		file, err := parser.ParseFile(fset, path, nil, 0)
		if err != nil {
			return fmt.Errorf("%s: %w", rel, err)
		}
		for _, r := range rules {
			if r.Kind == "import" || r.Kind == "forbid" {
				continue
			}
			if patternPresent(file, r) {
				found[r.ID] = true
			}
		}
		return nil
	})
	if err != nil {
		return err
	}
	for _, r := range rules {
		if r.Kind == "import" || r.Kind == "forbid" {
			continue
		}
		if !owners[r.ID] {
			return fmt.Errorf("%s rule %q: owner %q matches no Go file under %q; refused to pass",
				r.Kind, r.ID, r.Owner, root)
		}
		if !found[r.ID] {
			return fmt.Errorf("%s rule %q: pattern %q is absent from the scanned tree; the rule checks nothing",
				r.Kind, r.ID, r.Pattern)
		}
	}
	return nil
}

// patternPresent 报告一条规则的 pattern 是否在本文件里有落点。
// 判据与 inspectFile 的匹配口径一致，避免两处漂移。
func patternPresent(file *ast.File, r Rule) bool {
	present := false
	ast.Inspect(file, func(n ast.Node) bool {
		if present {
			return false
		}
		switch r.Kind {
		case "call":
			call, ok := n.(*ast.CallExpr)
			if !ok {
				return true
			}
			if name, ok := selectorName(call.Fun); ok && name == r.Pattern {
				present = true
			}
		case "func":
			if fn, ok := n.(*ast.FuncDecl); ok && fn.Name != nil && fn.Name.Name == r.Pattern {
				present = true
			}
		case "type":
			if ts, ok := n.(*ast.TypeSpec); ok && ts.Name != nil && ts.Name.Name == r.Pattern {
				present = true
			}
		case "string":
			lit, ok := n.(*ast.BasicLit)
			if !ok || lit.Kind != token.STRING || strings.HasPrefix(lit.Value, "`") {
				return true
			}
			value, err := strconv.Unquote(lit.Value)
			if err != nil {
				return true
			}
			for _, pattern := range strings.Split(r.Pattern, ",") {
				if value == pattern {
					present = true
					break
				}
			}
		}
		return true
	})
	return present
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
