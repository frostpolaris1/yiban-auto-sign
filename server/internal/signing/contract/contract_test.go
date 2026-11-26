package contract

import (
	"errors"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"sort"
	"strconv"
	"strings"
	"testing"
)

// TestOutcomeValid 要求词表每一项都合法。
// 清单来自 AllOutcomes：不再手抄一份，漏登记会在这里暴露。
func TestOutcomeValid(t *testing.T) {
	for _, o := range AllOutcomes() {
		if !o.Valid() {
			t.Fatalf("%q must be valid", o)
		}
	}
	if Outcome("done").Valid() {
		t.Fatal("unknown outcome must be invalid")
	}
}

// outcomeDecl 是一次 Outcome 常量声明（文件、偏移、常量名、字面量值）。
// 记文件名与偏移是为了在跨文件时也能稳定排序：顺序即契约结果表的顺序。
type outcomeDecl struct {
	file   string
	offset int
	name   string
	value  string
}

// TestAllOutcomesMatchDeclaration 用 AST 门钉住常量声明与 AllOutcomes 的一致性。
//
// 依据：契约结果表只能有一个来源。手抄清单漏项就是「空绿」的根因
// （本测试落地前的 TestOutcomeValid 就漏了 no_position）。
//
// 口径有意收紧到**整个包**：只解析 contract.go 时，把新常量写进
// outcome_extra.go 就能绕过门（本测试改动前实测为绿）。
// 现在扫包内全部非 _test.go 文件，且同时核对 Valid() 的 case 集合，
// 新增常量漏登记 AllOutcomes、漏加 Valid 分支、改动顺序或写入重复项都判红。
func TestAllOutcomesMatchDeclaration(t *testing.T) {
	declared := declaredOutcomes(t)
	listed := AllOutcomes()

	if len(declared) != len(listed) {
		t.Fatalf("常量声明 %d 项, AllOutcomes %d 项（两者必须一致）", len(declared), len(listed))
	}
	for i, o := range listed {
		if string(o) != declared[i].value {
			t.Fatalf("第 %d 项 = %q, 常量声明为 %q（%s:%d；顺序与取值都必须一致）",
				i+1, o, declared[i].value, declared[i].file, declared[i].offset)
		}
		if !o.Valid() {
			t.Fatalf("AllOutcomes 第 %d 项 %q 不合法", i+1, o)
		}
	}

	// 重复项会让数量相等但集合变小的漏项隐身。
	seen := make(map[Outcome]bool, len(listed))
	for _, o := range listed {
		if seen[o] {
			t.Fatalf("AllOutcomes 重复登记 %q", o)
		}
		seen[o] = true
	}

	// Valid() 必须恰好覆盖同一集合：常量加了却漏加 case 同样判红。
	cases := validCases(t, declared)
	if len(cases) != len(listed) {
		t.Fatalf("Valid() 的 case %d 项, AllOutcomes %d 项（两者必须一致）", len(cases), len(listed))
	}
	for _, value := range cases {
		if !seen[Outcome(value)] {
			t.Fatalf("Valid() 接受 %q，但它不在 AllOutcomes 内", value)
		}
	}
}

// declaredOutcomes 按（文件名，偏移）顺序收集包内全部 type Outcome 字符串常量。
// 只读非 _test.go：测试文件里的探针常量不参与契约。
func declaredOutcomes(t *testing.T) []outcomeDecl {
	t.Helper()
	fset := token.NewFileSet()
	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatalf("read 包目录: %v", err)
	}

	names := make([]string, 0, len(entries))
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		names = append(names, name)
	}
	sort.Strings(names)
	if len(names) == 0 {
		t.Fatal("包目录内没有可解析的 contract 源文件")
	}

	var decls []outcomeDecl
	for _, name := range names {
		file, err := parser.ParseFile(fset, name, nil, 0)
		if err != nil {
			t.Fatalf("parse %s: %v", name, err)
		}
		decls = append(decls, fileOutcomeDecls(t, fset, name, file)...)
	}
	sort.SliceStable(decls, func(i, j int) bool {
		if decls[i].file != decls[j].file {
			return decls[i].file < decls[j].file
		}
		return decls[i].offset < decls[j].offset
	})
	return decls
}

// fileOutcomeDecls 收集单个文件的 Outcome 常量。
// 同一个 const 块内省略类型时沿用上一项的类型（Go 的类型继承）。
func fileOutcomeDecls(t *testing.T, fset *token.FileSet, file string, node *ast.File) []outcomeDecl {
	t.Helper()
	var decls []outcomeDecl
	for _, decl := range node.Decls {
		gen, ok := decl.(*ast.GenDecl)
		if !ok || gen.Tok != token.CONST {
			continue
		}
		currentType := ""
		for _, spec := range gen.Specs {
			valueSpec, ok := spec.(*ast.ValueSpec)
			if !ok {
				continue
			}
			if valueSpec.Type != nil {
				ident, ok := valueSpec.Type.(*ast.Ident)
				if !ok {
					currentType = ""
				} else {
					currentType = ident.Name
				}
			}
			if currentType != "Outcome" {
				continue
			}
			for i, value := range valueSpec.Values {
				literal, ok := value.(*ast.BasicLit)
				if !ok || literal.Kind != token.STRING {
					t.Fatalf("%s 的 Outcome 常量不是字符串字面量：%T", file, value)
				}
				text, err := strconv.Unquote(literal.Value)
				if err != nil {
					t.Fatalf("解引号 %s: %v", literal.Value, err)
				}
				decls = append(decls, outcomeDecl{
					file:   file,
					offset: fset.Position(valueSpec.Pos()).Offset + i,
					name:   valueSpec.Names[min(i, len(valueSpec.Names)-1)].Name,
					value:  text,
				})
			}
		}
	}
	return decls
}

// validCases 返回 Valid() 的 switch 接受的词值，并校验每个 case 都是已声明常量。
func validCases(t *testing.T, declared []outcomeDecl) []string {
	t.Helper()
	byName := make(map[string]string, len(declared))
	for _, decl := range declared {
		byName[decl.name] = decl.value
	}

	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, "contract.go", nil, 0)
	if err != nil {
		t.Fatalf("parse contract.go: %v", err)
	}
	var valid *ast.FuncDecl
	for _, decl := range file.Decls {
		fn, ok := decl.(*ast.FuncDecl)
		if !ok || fn.Name.Name != "Valid" || fn.Body == nil {
			continue
		}
		valid = fn
		break
	}
	if valid == nil {
		t.Fatal("contract.go 里没有 Valid() 实现")
	}

	var values []string
	ast.Inspect(valid.Body, func(node ast.Node) bool {
		clause, ok := node.(*ast.CaseClause)
		if !ok {
			return true
		}
		for _, expr := range clause.List {
			ident, ok := expr.(*ast.Ident)
			if !ok {
				t.Fatalf("Valid() 的 case 不是常量名：%T", expr)
			}
			value, ok := byName[ident.Name]
			if !ok {
				t.Fatalf("Valid() 的 case %q 不是包内 Outcome 常量", ident.Name)
			}
			values = append(values, value)
		}
		return false // case 内部不再下钻
	})
	return values
}

func TestUnknownIsNotSuccess(t *testing.T) {
	if OutcomeUnknown == OutcomeSuccess {
		t.Fatal("unknown must stay distinct from success")
	}
}

func TestOutcomeError(t *testing.T) {
	withMessage := &OutcomeError{Outcome: OutcomeCredentialBad, Message: "bad credentials"}
	if withMessage.Error() != "bad credentials" {
		t.Fatalf("Error() = %q", withMessage.Error())
	}
	withoutMessage := &OutcomeError{Outcome: OutcomeChallenge}
	if withoutMessage.Error() != "provider: challenge" {
		t.Fatalf("Error() = %q", withoutMessage.Error())
	}
	var target *OutcomeError
	if !errors.As(error(withMessage), &target) || target != withMessage {
		t.Fatal("errors.As must recover the OutcomeError")
	}
}
