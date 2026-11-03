package logfield

import (
	"go/ast"
	"go/parser"
	"go/token"
	"strings"
	"testing"
)

// TestAllCoversEveryKeyConstant 钉住登记表与常量集合一致。
// 只读本文件源码：All 的每条登记都必须对应一个真实存在的导出 Key* 常量，
// 每个导出 Key* 常量必须恰好登记一行。多一行是无人使用的契约，少一行是漏登记。
func TestAllCoversEveryKeyConstant(t *testing.T) {
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, "fields.go", nil, 0)
	if err != nil {
		t.Fatalf("parse fields.go: %v", err)
	}
	consts := map[string]bool{}
	for _, decl := range file.Decls {
		gen, ok := decl.(*ast.GenDecl)
		if !ok || gen.Tok != token.CONST {
			continue
		}
		for _, spec := range gen.Specs {
			vs, ok := spec.(*ast.ValueSpec)
			if !ok {
				continue
			}
			for _, name := range vs.Names {
				if ast.IsExported(name.Name) && strings.HasPrefix(name.Name, "Key") {
					consts[name.Name] = true
				}
			}
		}
	}
	if len(consts) == 0 {
		t.Fatal("fields.go 没有导出 Key* 常量")
	}

	validKind := map[Kind]bool{
		KindID:       true,
		KindDay:      true,
		KindDuration: true,
		KindCount:    true,
		KindBool:     true,
		KindError:    true,
		KindEnum:     true,
		KindText:     true,
	}
	rows := map[string]int{}
	keys := map[string]string{}
	for _, f := range All {
		rows[f.Const]++
		if !consts[f.Const] {
			t.Errorf("All 登记了不存在的常量 %q", f.Const)
			continue
		}
		if f.Key == "" {
			t.Errorf("%s 的 Key 为空", f.Const)
			continue
		}
		if prev, ok := keys[f.Key]; ok {
			t.Errorf("键 %q 重复：%s 与 %s", f.Key, prev, f.Const)
		}
		keys[f.Key] = f.Const
		if !validKind[f.Kind] {
			t.Errorf("%s 的 Kind %q 不合法", f.Const, f.Kind)
		}
		if strings.TrimSpace(f.Meaning) == "" {
			t.Errorf("%s 的 Meaning 为空", f.Const)
		}
	}
	for name := range consts {
		if rows[name] != 1 {
			t.Errorf("常量 %s 在 All 中登记 %d 行，want 1", name, rows[name])
		}
	}
}
