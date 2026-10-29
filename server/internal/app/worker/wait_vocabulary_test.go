package worker

// 本文件钉住内部等待原因的封闭词表（工单 2c2-③）。
//
// # 判据
// 写进 sign_tasks.result 的等待原因一律以 wait_ 开头。
// 前缀就是「等待」与「业务结论」的分界：读库的人只凭前缀判定。
// 词表封闭：条数改动必须显式登记（见断言里的数字）。
//
// # 手段
// 扫描本包全部非测试源文件的字符串常量。新增等待原因漏了前缀即红；
// 值带 wait_ 前缀但命名不含 wait 同样红。扫描为空不得当绿。

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

func TestWaitReasonVocabulary(t *testing.T) {
	consts := stringConsts(t, ".")
	waits := 0
	for name, val := range consts {
		nameIsWait := strings.HasPrefix(name, "wait")
		valIsWait := strings.HasPrefix(val, "wait_")
		switch {
		case nameIsWait && !valIsWait:
			t.Errorf("%s = %q：命名是等待原因，值缺 wait_ 前缀", name, val)
		case valIsWait && !nameIsWait:
			t.Errorf("%s = %q：值带 wait_ 前缀，命名不含 wait", name, val)
		}
		if valIsWait {
			waits++
		}
	}
	// 词表封闭：当前五条。增删一条就要改这里的数字。
	if waits != 5 {
		t.Fatalf("扫描到 %d 条 wait_ 词，want 5（扫空或漏扫都不得当绿）", waits)
	}
}

// stringConsts 返回目录内非测试源文件的字符串常量：常量名 → 值。
// 只取字面量值；非常量表达式不在扫描面，本包无此形状。
func stringConsts(t *testing.T, dir string) map[string]string {
	t.Helper()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("read %s: %v", dir, err)
	}
	fset := token.NewFileSet()
	out := make(map[string]string)
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		file, err := parser.ParseFile(fset, filepath.Join(dir, name), nil, 0)
		if err != nil {
			t.Fatalf("parse %s: %v", name, err)
		}
		ast.Inspect(file, func(n ast.Node) bool {
			decl, ok := n.(*ast.GenDecl)
			if !ok || decl.Tok != token.CONST {
				return true
			}
			for _, spec := range decl.Specs {
				values, ok := spec.(*ast.ValueSpec)
				if !ok {
					continue
				}
				for i, expr := range values.Values {
					lit, ok := expr.(*ast.BasicLit)
					if !ok || lit.Kind != token.STRING || i >= len(values.Names) {
						continue
					}
					val, err := strconv.Unquote(lit.Value)
					if err != nil {
						continue
					}
					out[values.Names[i].Name] = val
				}
			}
			return false
		})
	}
	return out
}
