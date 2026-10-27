package pg_test

// 本文件钉住平台 ENUM 的库-码对照门（工单 zqv）。
// 词表在三处定义：迁移、core 常量、契约生成物。
// 任一侧改名或新增值即红。
// 需要真实 PostgreSQL：pg_enum 是库侧词表的唯一权威（AGENTS §32）。
// 契约侧的同集门在 internal/entry/http/oapi/enum_gate_test.go。
// adapters 不得导入 entry（canonical.tsv boundary.adapters），故三方对照拆成两段。
// 两段传递即三方同集。

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

// sourceEnum 描述一张 ENUM 的码侧定义点。
type sourceEnum struct {
	enum   string // pg 类型名
	dir    string // core 包目录，相对本包目录；整目录扫描
	typeID string // 常量显式类型名
	prefix string // 常量名前缀；同前缀但无显式类型即红
	min    int    // 常量数量下限，防空扫当绿
}

// platformEnums 是平台 ENUM 的对照表。
// 表本身就是覆盖面：schema 里的 ENUM 必须全在此表（TestSchemaEnumCoverage）。
// min 取自 771f5c0c 的 core 源文件实数。
var platformEnums = []sourceEnum{
	{"account_status", "../../core/account", "Status", "Status", 6},
	{"user_role", "../../core/auth", "Role", "Role", 3},
	{"user_status", "../../core/auth", "Status", "Status", 2},
	{"school_status", "../../core/school", "Status", "Status", 2},
	{"sign_task_state", "../../core/status", "TaskState", "Task", 5},
}

// enumLabels 返回当前 schema 中一个 ENUM 的标签，按 enumsortorder。
// current_schema 过滤：并发用例各占一个 schema。
// 不加过滤会跨 schema 拼接词表，偶发红。
func enumLabels(t *testing.T, e *testEnv, typeName string) []string {
	t.Helper()
	joined, err := e.db.QueryString(e.ctx,
		`SELECT string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder)
		 FROM pg_enum e
		 JOIN pg_type t ON t.oid = e.enumtypid
		 JOIN pg_namespace n ON n.oid = t.typnamespace
		 WHERE t.typname = $1 AND n.nspname = current_schema()`, typeName)
	if err != nil {
		t.Fatalf("query pg_enum %s: %v", typeName, err)
	}
	return strings.Split(joined, ",")
}

// schemaEnumNames 返回当前 schema 的 ENUM 类型名，按字母序。
// typtype='e' 只取 ENUM；数组类型是 'b'，不会混入。
func schemaEnumNames(t *testing.T, e *testEnv) []string {
	t.Helper()
	joined, err := e.db.QueryString(e.ctx,
		`SELECT string_agg(t.typname, ',' ORDER BY t.typname)
		 FROM pg_type t
		 JOIN pg_namespace n ON n.oid = t.typnamespace
		 WHERE t.typtype = 'e' AND n.nspname = current_schema()`)
	if err != nil {
		t.Fatalf("query pg_type enums: %v", err)
	}
	return strings.Split(joined, ",")
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
// 同形副本在 internal/entry/http/oapi/enum_gate_test.go。改一处必须同改另一处。
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

// sortedJoin 把取值归一为升序逗号串。
// 库侧按 enumsortorder 返回，core 按声明序，故按集合比对。
func sortedJoin(values []string) string {
	sorted := append([]string(nil), values...)
	sort.Strings(sorted)
	return strings.Join(sorted, ",")
}

// TestPlatformEnumLabelsMatchCore：每张 ENUM 的库侧标签等于 core 常量集合。
// 突变任一侧取值、或新增常量，即红（审查 D1）。
func TestPlatformEnumLabelsMatchCore(t *testing.T) {
	e := newEnv(t)
	for _, row := range platformEnums {
		t.Run(row.enum, func(t *testing.T) {
			want := sortedJoin(sourceEnumValues(t, row.dir, row.typeID, row.prefix, row.min))
			if got := sortedJoin(enumLabels(t, e, row.enum)); got != want {
				t.Fatalf("ENUM %s 词表 = %q, want %q（库与 core 词表漂移）", row.enum, got, want)
			}
		})
	}
}

// TestSchemaEnumCoverage：schema 内 ENUM 类型集合等于 platformEnums 的表名集合。
// 删掉表里任一行、或迁移新增 ENUM，都必须红（审查 D2）。
func TestSchemaEnumCoverage(t *testing.T) {
	e := newEnv(t)
	want := make([]string, 0, len(platformEnums))
	for _, row := range platformEnums {
		want = append(want, row.enum)
	}
	sort.Strings(want)
	if got := strings.Join(schemaEnumNames(t, e), ","); got != strings.Join(want, ",") {
		t.Fatalf("schema ENUM 集合 = %q, want %q（新增 ENUM 未配对照门，或表里有多余行）",
			got, strings.Join(want, ","))
	}
}
