// Command logfield 是日志字段键的门。
//
// # 功能
// 扫描 server 下全部非测试 Go 文件，要求每个结构化日志键都是
// logfield.Key* 常量：裸字符串、未知常量与动态表达式一律报红。
// 本工具**不**改代码、**不**判键语义、**不**改日志输出格式。
//
// # 归属
// server/tools 下的检查工具，由 scripts/verify.sh 在 canonical gate 之后调用。
//
// # 口径
// 识别的调用：接收者名含 log 的 Info/Warn/Error/Debug 及其 Context 变体
// （键从第 2 个实参起；*Context 变体跳过 ctx，从第 3 个实参起）
// 与 Log（键从第 4 个实参起）。`err.Error()`、`entry.Info()` 这类
// 非日志接收者不报。转发参数（如 `attrs...`）在同一函数内解析不到时，
// 调用行或上一行必须有 `//logfield:allow <理由>`。
// 含日志调用的函数里的 `[]any{...}` 复合字面量也按偶数位校验键。
// 空扫描（0 调用或 0 键）报错，不当绿。
package main

import (
	"flag"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/logfield"
)

func main() {
	root := flag.String("root", ".", "仓库 server 目录")
	flag.Parse()
	os.Exit(run(*root, os.Stdout, os.Stderr))
}

// keyConstants 是允许出现在日志键位置的常量名集合。
// 表为空时全部报未知：空登记不得让门放行。
var keyConstants = func() map[string]bool {
	m := make(map[string]bool, len(logfield.All))
	for _, f := range logfield.All {
		m[f.Const] = true
	}
	return m
}()

// violation 是一条门禁命中。
type violation struct {
	File string
	Line int
	Msg  string
}

// scanResult 是一次扫描的汇总。
type scanResult struct {
	Calls      int
	Keys       int
	Violations []violation
	seenKey    map[token.Pos]bool
	seenMsg    map[string]bool
}

// run 返回进程退出码。
// 它与 main 分离，使测试能用临时目录夹具钉住拒绝路径。
func run(root string, stdout, stderr io.Writer) int {
	res := &scanResult{
		seenKey: make(map[token.Pos]bool),
		seenMsg: make(map[string]bool),
	}
	if err := scanTree(root, res); err != nil {
		fmt.Fprintf(stderr, "logfield: %v\n", err)
		return 2
	}
	if res.Calls == 0 {
		fmt.Fprintf(stderr, "logfield: no log calls under %q; refused to pass\n", root)
		return 2
	}
	if len(res.Violations) > 0 {
		sort.Slice(res.Violations, func(i, j int) bool {
			if res.Violations[i].File != res.Violations[j].File {
				return res.Violations[i].File < res.Violations[j].File
			}
			return res.Violations[i].Line < res.Violations[j].Line
		})
		for _, v := range res.Violations {
			fmt.Fprintf(stdout, "%s:%d: %s\n", v.File, v.Line, v.Msg)
		}
		fmt.Fprintf(stderr, "logfield: %d violation(s)\n", len(res.Violations))
		return 1
	}
	if res.Keys == 0 {
		fmt.Fprintf(stderr, "logfield: no log field keys under %q; refused to pass\n", root)
		return 2
	}
	fmt.Fprintf(stdout, "logfield: ok (%d calls, %d keys)\n", res.Calls, res.Keys)
	return 0
}

// scanTree 遍历 root 下的非测试 Go 文件。
func scanTree(root string, res *scanResult) error {
	return filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, relErr := filepath.Rel(root, path)
		if relErr != nil {
			return relErr
		}
		rel = filepath.ToSlash(rel)
		if d.IsDir() {
			if skipDir(rel, d.Name()) {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(d.Name(), ".go") || strings.HasSuffix(d.Name(), "_test.go") {
			return nil
		}
		return scanFile(path, rel, res)
	})
}

// skipDir 报告某个目录是否整棵跳过。
// 键表自身的包与本工具不参与扫描：它们本来就写着键。
func skipDir(rel, name string) bool {
	switch name {
	case ".git", "work", "node_modules":
		return true
	}
	// -root 既可能是 server 目录，也可能是仓库根：统一按 server 内相对路径判定。
	trimmed := strings.TrimPrefix(rel, "server/")
	for _, skip := range []string{"internal/core/logfield", "tools/logfield"} {
		if trimmed == skip || strings.HasPrefix(trimmed, skip+"/") {
			return true
		}
	}
	return false
}

// scanFile 解析一个文件并逐个函数扫描。
func scanFile(path, rel string, res *scanResult) error {
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, path, nil, parser.ParseComments)
	if err != nil {
		return fmt.Errorf("%s: %w", rel, err)
	}
	allow := allowComments(fset, file)
	for _, decl := range file.Decls {
		fn, ok := decl.(*ast.FuncDecl)
		if !ok || fn.Body == nil {
			continue
		}
		scanFunc(fset, rel, fn.Body, allow, res)
	}
	return nil
}

// allowComments 收集 `//logfield:allow <理由>` 注释所在行。
func allowComments(fset *token.FileSet, file *ast.File) map[int]string {
	allow := map[int]string{}
	for _, group := range file.Comments {
		for _, c := range group.List {
			text := strings.TrimSpace(strings.TrimPrefix(c.Text, "//"))
			if !strings.HasPrefix(text, "logfield:allow") {
				continue
			}
			allow[fset.Position(c.Pos()).Line] = strings.TrimSpace(strings.TrimPrefix(text, "logfield:allow"))
		}
	}
	return allow
}

// scanFunc 扫描一个函数体。没有日志调用的函数整段跳过：
// 普通 `[]any{...}`（如 SQL 参数）不得被误判成日志键。
func scanFunc(fset *token.FileSet, file string, body *ast.BlockStmt, allow map[int]string, res *scanResult) {
	var calls []*ast.CallExpr
	ast.Inspect(body, func(n ast.Node) bool {
		if call, ok := n.(*ast.CallExpr); ok && isLogCall(call) {
			calls = append(calls, call)
		}
		return true
	})
	if len(calls) == 0 {
		return
	}
	for _, call := range calls {
		scanCall(fset, file, body, call, allow, res)
	}
	// 同函数内的切片构造也按日志键口径校验：转发到日志的键常在字面量里。
	ast.Inspect(body, func(n ast.Node) bool {
		if lit, ok := n.(*ast.CompositeLit); ok && isAnySlice(lit) {
			validatePairs(fset, file, lit.Elts, res)
		}
		return true
	})
}

// scanCall 校验一次日志调用的显式键与转发参数。
func scanCall(fset *token.FileSet, file string, body *ast.BlockStmt, call *ast.CallExpr, allow map[int]string, res *scanResult) {
	res.Calls++
	start := 1
	switch callName(call) {
	case "Log":
		start = 3
	case "InfoContext", "WarnContext", "ErrorContext", "DebugContext":
		// ctx、msg 之后才是键。
		start = 2
	}
	args := call.Args
	if call.Ellipsis != token.NoPos && len(args) > 0 {
		forwarded := args[len(args)-1]
		args = args[:len(args)-1]
		scanForward(fset, file, body, call, forwarded, allow, res)
	}
	if len(args) <= start {
		return
	}
	pairs := args[start:]
	for i := 0; i < len(pairs); i += 2 {
		validateKey(fset, file, pairs[i], res)
	}
	if len(pairs)%2 != 0 {
		addViolation(fset, file, call.Pos(), res, "日志键缺少值")
	}
}

// scanForward 处理 `attrs...` 一类转发参数。
// 同函数内解析得到切片构造就逐对校验；解析不到则要求 allow 注释。
func scanForward(fset *token.FileSet, file string, body *ast.BlockStmt, call *ast.CallExpr, forwarded ast.Expr, allow map[int]string, res *scanResult) {
	ident, ok := forwarded.(*ast.Ident)
	if ok && resolveSlice(fset, file, body, ident.Name, res) {
		return
	}
	line := fset.Position(call.Pos()).Line
	if reason, found := allowReason(allow, line); found {
		if strings.TrimSpace(reason) == "" {
			addViolationLine(file, line, res, "//logfield:allow 缺少理由")
		}
		return
	}
	name := "转发参数"
	if ident != nil {
		name = ident.Name + "..."
	}
	addViolationLine(file, line, res,
		fmt.Sprintf("%s 需要 //logfield:allow <理由>（调用行或上一行）", name))
}

// allowReason 查调用行或上一行的 allow 注释。
func allowReason(allow map[int]string, line int) (string, bool) {
	for _, l := range []int{line, line - 1} {
		if reason, ok := allow[l]; ok {
			return reason, true
		}
	}
	return "", false
}

// resolveSlice 在同一函数内找 name 的切片构造并校验键位。
// 返回 false 表示解析不到（参数或动态值），调用方要求 allow。
func resolveSlice(fset *token.FileSet, file string, body *ast.BlockStmt, name string, res *scanResult) bool {
	resolved := false
	ast.Inspect(body, func(n ast.Node) bool {
		as, ok := n.(*ast.AssignStmt)
		if !ok || len(as.Rhs) != 1 {
			return true
		}
		matched := false
		for _, lhs := range as.Lhs {
			if id, ok := lhs.(*ast.Ident); ok && id.Name == name {
				matched = true
			}
		}
		if !matched {
			return true
		}
		switch rhs := as.Rhs[0].(type) {
		case *ast.CompositeLit:
			if isAnySlice(rhs) {
				validatePairs(fset, file, rhs.Elts, res)
				resolved = true
			}
		case *ast.CallExpr:
			if !isAppend(rhs) || len(rhs.Args) == 0 {
				return true
			}
			if rhs.Ellipsis != token.NoPos {
				// append(x, other...)：尾部是转发切片，不是键值对；
				// 头部若是字面量仍按对校验。
				if lit, ok := rhs.Args[0].(*ast.CompositeLit); ok && isAnySlice(lit) {
					validatePairs(fset, file, lit.Elts, res)
					resolved = true
				}
				return true
			}
			switch first := rhs.Args[0].(type) {
			case *ast.CompositeLit:
				if isAnySlice(first) {
					validatePairs(fset, file, first.Elts, res)
					resolved = true
				}
			case *ast.Ident:
				// append(attrs, "k", v)：追加的实参从下标 1 起成对。
				if first.Name == name {
					validatePairs(fset, file, rhs.Args[1:], res)
					resolved = true
				}
			}
		}
		return true
	})
	return resolved
}

// validatePairs 校验一对对实参里的键位（下标 0、2、4...）。
func validatePairs(fset *token.FileSet, file string, elems []ast.Expr, res *scanResult) {
	for i := 0; i < len(elems); i += 2 {
		validateKey(fset, file, elems[i], res)
	}
}

// validateKey 要求键是已登记的 logfield.Key* 选择器。
func validateKey(fset *token.FileSet, file string, e ast.Expr, res *scanResult) {
	switch v := e.(type) {
	case *ast.BasicLit:
		if v.Kind == token.STRING {
			addViolation(fset, file, e.Pos(), res,
				"日志键必须是 logfield.Key* 常量，不能用字符串字面量 "+v.Value)
			return
		}
	case *ast.SelectorExpr:
		if id, ok := v.X.(*ast.Ident); ok && id.Name == "logfield" {
			if !strings.HasPrefix(v.Sel.Name, "Key") {
				addViolation(fset, file, e.Pos(), res,
					"日志键必须是 logfield.Key* 常量，不能用 logfield."+v.Sel.Name)
				return
			}
			if !keyConstants[v.Sel.Name] {
				addViolation(fset, file, e.Pos(), res,
					"未知日志常量 logfield."+v.Sel.Name+"；先在 core/logfield 登记")
				return
			}
			countKey(res, e.Pos())
			return
		}
	case *ast.ParenExpr:
		validateKey(fset, file, v.X, res)
		return
	}
	addViolation(fset, file, e.Pos(), res, "日志键必须是 logfield.Key* 常量，不能用动态表达式")
}

// isLogCall 报告是否为受检日志调用。
// 接收者名含 log 才算：`err.Error()`、`entry.Info()` 因此不误报。
func isLogCall(call *ast.CallExpr) bool {
	sel, ok := call.Fun.(*ast.SelectorExpr)
	if !ok {
		return false
	}
	switch sel.Sel.Name {
	case "Info", "Warn", "Error", "Debug", "Log",
		"InfoContext", "WarnContext", "ErrorContext", "DebugContext":
	default:
		return false
	}
	return strings.Contains(strings.ToLower(receiverName(sel.X)), "log")
}

// callName 返回方法名。
func callName(call *ast.CallExpr) string {
	sel, ok := call.Fun.(*ast.SelectorExpr)
	if !ok {
		return ""
	}
	return sel.Sel.Name
}

// receiverName 把接收者表达式压成点分名，如 s.log。
func receiverName(e ast.Expr) string {
	switch v := e.(type) {
	case *ast.Ident:
		return v.Name
	case *ast.SelectorExpr:
		return receiverName(v.X) + "." + v.Sel.Name
	case *ast.ParenExpr:
		return receiverName(v.X)
	case *ast.IndexExpr:
		return receiverName(v.X)
	default:
		return ""
	}
}

// isAnySlice 报告字面量是否为 `[]any{...}` 或 `[]interface{}{...}`。
func isAnySlice(lit *ast.CompositeLit) bool {
	arr, ok := lit.Type.(*ast.ArrayType)
	if !ok || arr.Len != nil {
		return false
	}
	switch elt := arr.Elt.(type) {
	case *ast.Ident:
		return elt.Name == "any"
	case *ast.InterfaceType:
		return elt.Methods == nil || len(elt.Methods.List) == 0
	}
	return false
}

// isAppend 报告调用是否为内建 append。
func isAppend(call *ast.CallExpr) bool {
	id, ok := call.Fun.(*ast.Ident)
	return ok && id.Name == "append"
}

// countKey 按位置计数：同一键表达式只计一次（调用与切片两遍扫描会重合）。
func countKey(res *scanResult, pos token.Pos) {
	if res.seenKey[pos] {
		return
	}
	res.seenKey[pos] = true
	res.Keys++
}

// addViolation 按位置记录一条命中。
func addViolation(fset *token.FileSet, file string, pos token.Pos, res *scanResult, msg string) {
	addViolationLine(file, fset.Position(pos).Line, res, msg)
}

// addViolationLine 按行记录一条命中；同一位置同一说明只留一条。
func addViolationLine(file string, line int, res *scanResult, msg string) {
	key := file + "\x00" + fmt.Sprint(line) + "\x00" + msg
	if res.seenMsg[key] {
		return
	}
	res.seenMsg[key] = true
	res.Violations = append(res.Violations, violation{File: file, Line: line, Msg: msg})
}
