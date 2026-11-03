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
// 含日志调用的函数、以及导入了键表的文件的全部函数，其 `[]any{...}`
// 字面量与 append 键值对按偶数位校验；helper 返回的切片在可解析时
// 递归到同包函数体。门只做语法层数据流，不解析类型与包。
// 空扫描（0 调用或 0 键）报错，不当绿。
//
// # 已知边界
// 以下形状不在覆盖内，由独立审查兜底：
//   - LogAttrs 与 With；接收者名不含 log 的转发器；
//   - helper 内的 var/make 底座、返回链、参数底座与跨包 helper 的 append 尾部；
//   - 同名函数/方法按名递归，可能扫到未被调用的同名体（假阳性）；
//   - 导入键表的文件里的非日志 []any 字面量也按键位校验（假阳性）。
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
	"strconv"
	"strings"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/logfield"
)

func main() {
	root := flag.String("root", ".", "仓库 server 目录")
	flag.Parse()
	os.Exit(run(*root, os.Stdout, os.Stderr))
}

// logfieldImportPath 是键表自身的导入路径：导入了它的文件整文件扫描。
const logfieldImportPath = "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/logfield"

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
	// seenKey 用「文件:偏移」而不是 token.Pos：每个文件各用独立 FileSet，
	// 裸 Pos 跨文件会碰撞，导致键数少算。
	seenKey map[string]bool
	seenMsg map[string]bool
}

// run 返回进程退出码。
// 它与 main 分离，使测试能用临时目录夹具钉住拒绝路径。
func run(root string, stdout, stderr io.Writer) int {
	res := &scanResult{
		seenKey: make(map[string]bool),
		seenMsg: make(map[string]bool),
	}
	if err := scanTree(root, res); err != nil {
		_, _ = fmt.Fprintf(stderr, "logfield: %v\n", err)
		return 2
	}
	if res.Calls == 0 {
		_, _ = fmt.Fprintf(stderr, "logfield: no log calls under %q; refused to pass\n", root)
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
			_, _ = fmt.Fprintf(stdout, "%s:%d: %s\n", v.File, v.Line, v.Msg)
		}
		_, _ = fmt.Fprintf(stderr, "logfield: %d violation(s)\n", len(res.Violations))
		return 1
	}
	if res.Keys == 0 {
		_, _ = fmt.Fprintf(stderr, "logfield: no log field keys under %q; refused to pass\n", root)
		return 2
	}
	_, _ = fmt.Fprintf(stdout, "logfield: ok (%d calls, %d keys)\n", res.Calls, res.Keys)
	return 0
}

// scanTree 按目录遍历 root：同包函数要能互相解析（helper 递归）。
func scanTree(root string, res *scanResult) error {
	return filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !d.IsDir() {
			return nil
		}
		rel, relErr := filepath.Rel(root, path)
		if relErr != nil {
			return relErr
		}
		rel = filepath.ToSlash(rel)
		if skipDir(rel, d.Name()) {
			return filepath.SkipDir
		}
		return scanDir(path, rel, res)
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

// sourceFile 是一个已解析的非测试 Go 文件。
type sourceFile struct {
	rel  string
	fset *token.FileSet
	file *ast.File
	// importsLogfield 决定是否整文件扫描：attrs 常由不打日志的 helper 构造。
	importsLogfield bool
}

// funcEntry 是同包函数/方法索引项，供 helper 递归。
type funcEntry struct {
	sf *sourceFile
	fn *ast.FuncDecl
}

// scanDir 解析同一目录（同一包）的全部非测试文件后整体扫描。
func scanDir(dir, relDir string, res *scanResult) error {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return err
	}
	var files []*sourceFile
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		rel := name
		if relDir != "." {
			rel = relDir + "/" + name
		}
		sf, err := parseSourceFile(filepath.Join(dir, name), rel)
		if err != nil {
			return err
		}
		files = append(files, sf)
	}
	if len(files) == 0 {
		return nil
	}
	index := make(map[string][]funcEntry, len(files))
	for _, sf := range files {
		for _, decl := range sf.file.Decls {
			fn, ok := decl.(*ast.FuncDecl)
			if !ok || fn.Body == nil {
				continue
			}
			index[fn.Name.Name] = append(index[fn.Name.Name], funcEntry{sf: sf, fn: fn})
		}
	}
	for _, sf := range files {
		scanSourceFile(sf, index, res)
	}
	return nil
}

// parseSourceFile 解析一个文件，并记录它是否导入了键表。
func parseSourceFile(path, rel string) (*sourceFile, error) {
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, path, nil, parser.ParseComments)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", rel, err)
	}
	return &sourceFile{
		rel:             rel,
		fset:            fset,
		file:            file,
		importsLogfield: importsPackage(file, logfieldImportPath),
	}, nil
}

// importsPackage 报告文件是否导入指定包路径。
func importsPackage(file *ast.File, path string) bool {
	for _, imp := range file.Imports {
		got, err := strconv.Unquote(imp.Path.Value)
		if err == nil && got == path {
			return true
		}
	}
	return false
}

// scanSourceFile 逐函数扫描一个文件。
// 导入了键表的文件整文件扫描：不打日志的 helper 也常构造 attrs。
func scanSourceFile(sf *sourceFile, index map[string][]funcEntry, res *scanResult) {
	allow := allowComments(sf.fset, sf.file)
	for _, decl := range sf.file.Decls {
		fn, ok := decl.(*ast.FuncDecl)
		if !ok || fn.Body == nil {
			continue
		}
		if !sf.importsLogfield && !hasLogCall(fn.Body) {
			continue
		}
		scanFunc(sf, fn.Body, index, allow, res)
	}
}

// hasLogCall 报告函数体内是否有受检日志调用。
func hasLogCall(body *ast.BlockStmt) bool {
	found := false
	ast.Inspect(body, func(n ast.Node) bool {
		if found {
			return false
		}
		if call, ok := n.(*ast.CallExpr); ok && isLogCall(call) {
			found = true
		}
		return true
	})
	return found
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

// scanFunc 扫描一个函数体：日志调用、`[]any{...}` 字面量与 append 尾部。
// 调用方已按「文件导入键表」或「函数内有日志调用」决定是否进来。
func scanFunc(sf *sourceFile, body *ast.BlockStmt, index map[string][]funcEntry, allow map[int]string, res *scanResult) {
	var calls []*ast.CallExpr
	ast.Inspect(body, func(n ast.Node) bool {
		if call, ok := n.(*ast.CallExpr); ok && isLogCall(call) {
			calls = append(calls, call)
		}
		return true
	})
	for _, call := range calls {
		scanCall(sf.fset, sf.rel, body, call, allow, res)
	}
	scanAttrs(sf.fset, sf.rel, body, index, map[*ast.FuncDecl]bool{}, res)
}

// scanAttrs 校验 attrs 构造：`[]any{...}` 字面量与 append 的键值对。
// helper 返回的切片经 `append(x, helper(...)...)` 递归到同包函数体，
// visited 防自递归/互递归。
func scanAttrs(fset *token.FileSet, file string, body *ast.BlockStmt, index map[string][]funcEntry, visited map[*ast.FuncDecl]bool, res *scanResult) {
	names := anySliceNames(body)
	ast.Inspect(body, func(n ast.Node) bool {
		if lit, ok := n.(*ast.CompositeLit); ok && isAnySlice(lit) {
			validatePairs(fset, file, lit.Elts, res)
		}
		return true
	})
	ast.Inspect(body, func(n ast.Node) bool {
		call, ok := n.(*ast.CallExpr)
		if !ok || !isAppend(call) || len(call.Args) == 0 {
			return true
		}
		if !isAnySliceBase(call.Args[0], names) {
			return true
		}
		if call.Ellipsis == token.NoPos {
			// append(attrs, "key", val)：尾部实参从下标 1 起成对。
			validatePairs(fset, file, call.Args[1:], res)
			return true
		}
		forwarded, ok := call.Args[len(call.Args)-1].(*ast.CallExpr)
		if !ok {
			return true
		}
		scanHelper(forwarded, index, visited, res)
		return true
	})
}

// scanHelper 递归扫描同包 helper 的 attrs 构造。
func scanHelper(call *ast.CallExpr, index map[string][]funcEntry, visited map[*ast.FuncDecl]bool, res *scanResult) {
	name := calleeName(call.Fun)
	if name == "" {
		return
	}
	for _, entry := range index[name] {
		if visited[entry.fn] {
			continue
		}
		visited[entry.fn] = true
		scanAttrs(entry.sf.fset, entry.sf.rel, entry.fn.Body, index, visited, res)
	}
}

// calleeName 返回函数/方法名：`scene(...)` 与 `s.scene(...)` 都得 scene。
func calleeName(fun ast.Expr) string {
	switch v := fun.(type) {
	case *ast.Ident:
		return v.Name
	case *ast.SelectorExpr:
		return v.Sel.Name
	}
	return ""
}

// anySliceNames 收集函数内可判定为 `[]any` 的变量名。
// 只认明确来源：`var x []any`、`x := []any{...}`、以及根在二者上的 append。
// 认不出的变量不当 attrs，避免把 `[]string` 的 append 误判成键值对。
func anySliceNames(body *ast.BlockStmt) map[string]bool {
	names := map[string]bool{}
	for changed := true; changed; {
		changed = false
		mark := func(name string) {
			if name == "" || names[name] {
				return
			}
			names[name] = true
			changed = true
		}
		ast.Inspect(body, func(n ast.Node) bool {
			switch v := n.(type) {
			case *ast.DeclStmt:
				gen, ok := v.Decl.(*ast.GenDecl)
				if !ok || gen.Tok != token.VAR {
					return true
				}
				for _, spec := range gen.Specs {
					vs, ok := spec.(*ast.ValueSpec)
					if !ok || vs.Type == nil || !isAnySliceType(vs.Type) {
						continue
					}
					for _, name := range vs.Names {
						mark(name.Name)
					}
				}
			case *ast.AssignStmt:
				if len(v.Rhs) != 1 {
					return true
				}
				if !isAnySliceValue(v.Rhs[0], names) {
					return true
				}
				for _, lhs := range v.Lhs {
					if id, ok := lhs.(*ast.Ident); ok {
						mark(id.Name)
					}
				}
			}
			return true
		})
	}
	return names
}

// isAnySliceValue 报告表达式是否明确产出 `[]any`。
func isAnySliceValue(e ast.Expr, names map[string]bool) bool {
	switch v := e.(type) {
	case *ast.CompositeLit:
		return isAnySlice(v)
	case *ast.CallExpr:
		if !isAppend(v) || len(v.Args) == 0 {
			return false
		}
		return isAnySliceBase(v.Args[0], names)
	}
	return false
}

// isAnySliceBase 报告 append 的底座是否为 `[]any`。
func isAnySliceBase(e ast.Expr, names map[string]bool) bool {
	switch v := e.(type) {
	case *ast.CompositeLit:
		return isAnySlice(v)
	case *ast.Ident:
		return names[v.Name]
	}
	return false
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
			countKey(res, fset, file, e.Pos())
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
	return isAnySliceType(lit.Type)
}

// isAnySliceType 报告类型表达式是否为 `[]any` / `[]interface{}`。
func isAnySliceType(e ast.Expr) bool {
	arr, ok := e.(*ast.ArrayType)
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
// 键用「文件:偏移」：各文件独立 FileSet，裸 token.Pos 跨文件会碰撞。
func countKey(res *scanResult, fset *token.FileSet, file string, pos token.Pos) {
	k := file + ":" + strconv.Itoa(fset.Position(pos).Offset)
	if res.seenKey[k] {
		return
	}
	res.seenKey[k] = true
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
