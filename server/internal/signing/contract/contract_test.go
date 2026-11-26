package contract

import (
	"errors"
	"go/ast"
	"go/parser"
	"go/token"
	"strconv"
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

// TestAllOutcomesMatchDeclaration 用 AST 门钉住常量块与 AllOutcomes 的一致性。
//
// 依据：契约结果表只能有一个来源。手抄清单漏项就是「空绿」的根因
// （本测试落地前的 TestOutcomeValid 就漏了 no_position）。
// 新增 Outcome 常量却漏登记 AllOutcomes、改动顺序或写入重复项，
// 本测试都判红，且失败信息指出具体位置。
func TestAllOutcomesMatchDeclaration(t *testing.T) {
	file, err := parser.ParseFile(token.NewFileSet(), "contract.go", nil, 0)
	if err != nil {
		t.Fatalf("parse contract.go: %v", err)
	}
	declared := declaredOutcomes(t, file)
	listed := AllOutcomes()

	if len(declared) != len(listed) {
		t.Fatalf("常量块 %d 项, AllOutcomes %d 项（两者必须一致）", len(declared), len(listed))
	}
	for i, o := range listed {
		if string(o) != declared[i] {
			t.Fatalf("第 %d 项 = %q, 常量块为 %q（顺序与取值都必须一致）", i+1, o, declared[i])
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
}

// declaredOutcomes 按声明顺序收集 type Outcome 的全部字符串常量值。
// 同一个 const 块内省略类型时沿用上一项的类型（Go 的类型继承）。
func declaredOutcomes(t *testing.T, file *ast.File) []string {
	t.Helper()
	var values []string
	for _, decl := range file.Decls {
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
			for _, value := range valueSpec.Values {
				literal, ok := value.(*ast.BasicLit)
				if !ok || literal.Kind != token.STRING {
					t.Fatalf("Outcome 常量不是字符串字面量：%T", value)
				}
				text, err := strconv.Unquote(literal.Value)
				if err != nil {
					t.Fatalf("解引号 %s: %v", literal.Value, err)
				}
				values = append(values, text)
			}
		}
	}
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
