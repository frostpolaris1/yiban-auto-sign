package oapi_test

// 本文件钉住契约枚举与码侧词表的同集门（工单 zqv）。
// 词表在三处定义：迁移、core 或 signing/contract 常量、契约生成物。
// 有可扫描显式类型词表的契约枚举登记在 contractEnums，并断言同集。
// 其余契约枚举登记在 contractEnumExempt，逐条写明理由（D8）。
// 常量从源文件读：oapi.gen.go 是生成物，也不手抄（审查 D1）。
// 库侧与 core 的对照门在 internal/adapters/pg/enum_gate_e2e_test.go。
// adapters 不得导入 entry（canonical.tsv boundary.adapters），故三方对照拆成两段。
// 本文件不碰数据库，默认测试即执行，无 DSN 也跑。

import (
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"sort"
	"strconv"
	"strings"
	"testing"
)

// contractEnum 描述一张契约枚举与对应的码侧词表（core 或 signing/contract）。
type contractEnum struct {
	apiDir   string // oapi 包目录；整目录扫描
	apiType  string // oapi 常量显式类型名
	dir      string // 码侧包目录（core 或 signing/contract），相对本包目录；整目录扫描
	coreType string // 码侧常量显式类型名
	prefix   string // 码侧常量名前缀；同前缀但无显式类型即红
	min      int    // 两侧常量数量下限，防空扫当绿
}

// contractEnums 是契约枚举的对照表。
// min 取自 771f5c0c 的 oapi.gen.go 与码侧源文件实数。
var contractEnums = []contractEnum{
	{".", "AccountDiagnosticsWindowSource", "../../../signing/contract", "WindowSource", "Window", 4},
	{".", "AccountLocationMode", "../../../signing/contract", "LocationMode", "Location", 2},
	{".", "AccountStatus", "../../../core/account", "Status", "Status", 6},
	{".", "ErrorCode", "../../../core/errs", "Code", "Code", 16},
	{".", "ScheduleWindowSource", "../../../signing/contract", "WindowSource", "Window", 4},
	{".", "SchoolStatus", "../../../core/school", "Status", "Status", 2},
	{".", "UserRole", "../../../core/auth", "Role", "Role", 3},
	{".", "UserStatus", "../../../core/auth", "Status", "Status", 2},
}

// contractExempt 是一条豁免登记。
type contractExempt struct {
	apiType string
	reason  string
}

// contractEnumExempt 列出不设码侧对照的契约枚举类型与理由。
// 差集口径：oapi.gen.go 带字符串常量的类型全集 - contractEnums。
// 统一理由：无 pg ENUM；core 与 signing/contract 的显式类型词表必须进 contractEnums。
var contractEnumExempt = []contractExempt{
	{"AccountDiagnosticsSuggestion", "core 词表未标注类型，扫描器不覆盖（core/diagnostic.Suggest*）"},
	{"AccountLocationUpdateMode", "纯输入枚举，无转换写出点"},
	{"AccountReviewRequestDecision", "无 pg ENUM"},
	{"DiagnosticsItemSuggestion", "core 词表未标注类型，扫描器不覆盖（core/diagnostic.Suggest*）"},
	{"HealthStatusStatus", "无 pg ENUM"},
	{"PhotoMime", "core 无显式类型；库侧 CHECK 为同 2 值"},
	{"SchoolCreateTimezone", "无 pg ENUM"},
	{"SchoolUpdateTimezone", "无 pg ENUM"},
}

// walkConsts 解析 dir 下全部非 _test.go 的 .go 文件。
// 文件按名序、常量按声明序回调 visit，结果稳定。
// var 声明不扫：词表来源必须是 const（D7 豁免）。
// 词表可以分多文件；只看单文件会漏掉新增常量（D4）。
func walkConsts(t *testing.T, dir string, visit func(*ast.ValueSpec)) {
	t.Helper()
	pkgs, err := parser.ParseDir(token.NewFileSet(), dir, func(fi fs.FileInfo) bool {
		name := fi.Name()
		return strings.HasSuffix(name, ".go") && !strings.HasSuffix(name, "_test.go")
	}, 0)
	if err != nil {
		t.Fatalf("parse dir %s: %v", dir, err)
	}
	files := map[string]*ast.File{}
	for _, pkg := range pkgs {
		for name, file := range pkg.Files {
			files[name] = file
		}
	}
	names := make([]string, 0, len(files))
	for name := range files {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		for _, decl := range files[name].Decls {
			gen, ok := decl.(*ast.GenDecl)
			if !ok || gen.Tok != token.CONST {
				continue
			}
			for _, spec := range gen.Specs {
				if value, ok := spec.(*ast.ValueSpec); ok {
					visit(value)
				}
			}
		}
	}
}

// sourceEnumValues 读出 dir 内显式声明为 typeID 的字符串常量，按声明序。
// 常量从源文件读，故新增常量无需改测试（审查 D1）。
// 同形副本在 internal/adapters/pg/enum_gate_e2e_test.go。改一处必须同改另一处。
func sourceEnumValues(t *testing.T, dir, typeID, prefix string, min int) []string {
	t.Helper()
	var out []string
	walkConsts(t, dir, func(value *ast.ValueSpec) {
		if !declaresType(value, typeID) {
			// 无显式类型的词表常量扫不到，必须红。
			// 前缀命中算一种（D5）；无前缀的转换式也算（R2）。
			if value.Type == nil {
				if prefixedName(value, prefix) {
					t.Fatalf("%s: const %s has prefix %s but no explicit type %s",
						dir, value.Names[0].Name, prefix, typeID)
				}
				if convertsTo(value, typeID) {
					t.Fatalf("%s: const %s is a %s conversion without an explicit type",
						dir, value.Names[0].Name, typeID)
				}
			}
			return
		}
		for _, expr := range value.Values {
			lit, ok := expr.(*ast.BasicLit)
			if !ok || lit.Kind != token.STRING {
				t.Fatalf("%s: %s const %s is not a string literal", dir, typeID, value.Names[0].Name)
			}
			text, err := strconv.Unquote(lit.Value)
			if err != nil {
				t.Fatalf("unquote %s: %v", lit.Value, err)
			}
			out = append(out, text)
		}
	})
	if len(out) < min {
		t.Fatalf("%s: found %d %s constants; want at least %d (scan is wrong or a constant was removed)",
			dir, len(out), typeID, min)
	}
	return out
}

// declaresType 报告常量是否显式标注 typeID。
func declaresType(value *ast.ValueSpec, typeID string) bool {
	ident, ok := value.Type.(*ast.Ident)
	return ok && ident.Name == typeID
}

// prefixedName 报告常量名是否带词表前缀。
func prefixedName(value *ast.ValueSpec, prefix string) bool {
	for _, name := range value.Names {
		if strings.HasPrefix(name.Name, prefix) {
			return true
		}
	}
	return false
}

// convertsTo 报告常量值是否为 typeID(...) 形式的转换调用（R2）。
func convertsTo(value *ast.ValueSpec, typeID string) bool {
	for _, expr := range value.Values {
		call, ok := expr.(*ast.CallExpr)
		if !ok {
			continue
		}
		if ident, ok := call.Fun.(*ast.Ident); ok && ident.Name == typeID {
			return true
		}
	}
	return false
}

// contractConstTypes 返回 dir 内带字符串取值的显式类型名，按字母序。
// 无显式类型的转换式常量按函数名记类型（R3）。
// 空扫不得当绿（审查 D6）。
func contractConstTypes(t *testing.T, dir string) []string {
	t.Helper()
	seen := map[string]bool{}
	walkConsts(t, dir, func(value *ast.ValueSpec) {
		name, ok := constTypeName(value)
		if !ok || !hasStringValue(value) {
			return
		}
		seen[name] = true
	})
	out := make([]string, 0, len(seen))
	for name := range seen {
		out = append(out, name)
	}
	sort.Strings(out)
	if len(out) == 0 {
		t.Fatalf("%s: found no string const types; the scan is wrong", dir)
	}
	return out
}

// constTypeName 返回常量声明的类型名。
// 显式类型优先；无显式类型时取转换式函数名（R3）。
func constTypeName(value *ast.ValueSpec) (string, bool) {
	if ident, ok := value.Type.(*ast.Ident); ok {
		return ident.Name, true
	}
	if value.Type != nil || len(value.Values) != 1 {
		return "", false
	}
	call, ok := value.Values[0].(*ast.CallExpr)
	if !ok {
		return "", false
	}
	ident, ok := call.Fun.(*ast.Ident)
	if !ok {
		return "", false
	}
	return ident.Name, true
}

// hasStringValue 报告常量值含字符串字面量或类型转换调用（R3）。
func hasStringValue(value *ast.ValueSpec) bool {
	for _, expr := range value.Values {
		switch e := expr.(type) {
		case *ast.BasicLit:
			if e.Kind == token.STRING {
				return true
			}
		case *ast.CallExpr:
			if _, ok := e.Fun.(*ast.Ident); ok {
				return true
			}
		}
	}
	return false
}

// sortedJoin 把取值归一为升序逗号串。
// 契约生成物按字母序排列常量，core 按声明序，故按集合比对。
func sortedJoin(values []string) string {
	sorted := append([]string(nil), values...)
	sort.Strings(sorted)
	return strings.Join(sorted, ",")
}

// TestContractEnumsMatchCore：契约枚举取值等于码侧常量集合。
// 突变任一侧取值、或新增常量，即红（审查 D1）。
func TestContractEnumsMatchCore(t *testing.T) {
	for _, row := range contractEnums {
		t.Run(row.apiType, func(t *testing.T) {
			api := sourceEnumValues(t, row.apiDir, row.apiType, row.apiType, row.min)
			core := sourceEnumValues(t, row.dir, row.coreType, row.prefix, row.min)
			if got, want := sortedJoin(api), sortedJoin(core); got != want {
				t.Fatalf("%s 契约取值 = %q, want %q（契约与码侧词表漂移）", row.apiType, got, want)
			}
		})
	}
}

// TestContractEnumCoverage：契约枚举全集 = 对照表 + 豁免表。
// 新增契约枚举未登记即红（审查 D6）。
func TestContractEnumCoverage(t *testing.T) {
	want := make([]string, 0, len(contractEnums)+len(contractEnumExempt))
	for _, row := range contractEnums {
		want = append(want, row.apiType)
	}
	for _, row := range contractEnumExempt {
		if row.reason == "" {
			t.Fatalf("%s 的豁免没有理由", row.apiType)
		}
		want = append(want, row.apiType)
	}
	sort.Strings(want)
	if got := strings.Join(contractConstTypes(t, "."), ","); got != strings.Join(want, ",") {
		t.Fatalf("契约枚举全集 = %q, want %q（新增契约枚举未登记对照或豁免）",
			got, strings.Join(want, ","))
	}
}
