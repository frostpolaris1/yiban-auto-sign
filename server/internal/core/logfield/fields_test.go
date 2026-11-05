package logfield

import (
	"go/ast"
	"go/parser"
	"go/token"
	"slices"
	"sort"
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
		KindNumber:   true,
		KindText:     true,
		KindMap:      true,
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

// TestKeysArePinned 钉住键面本身：键串是日志读者的公开接口。
// 只改常量值（如 KeyError = "err"）而不改调用点，其它测试仍绿；本测试会红。
// 增删键是契约变更：常量、All 与这份 golden 必须同批改。
func TestKeysArePinned(t *testing.T) {
	// golden 必须写死键串；原始串避免 status.literal 把键名误判成状态逻辑。
	golden := []string{
		"account_id",
		"attempt",
		"batch",
		"blobs_deleted",
		"built",
		"bytes",
		"count",
		"credential_kid",
		"day",
		"duration_ms",
		"email",
		"error",
		"event",
		`failed`,
		"failed_days",
		"history_available",
		"host",
		"kind",
		"last_success_day",
		"method",
		"notifications",
		"outcome",
		"outcome_counts",
		"panic",
		"path",
		`pending`,
		"poll_interval",
		"port",
		"reason",
		"remaining",
		"request_id",
		"reset_tokens",
		"retryable",
		"rows_deleted",
		"school_id",
		"school_task_id",
		"sessions",
		`skipped`,
		"stage",
		"status",
		`success`,
		"suggestion",
		"task_id",
		"tasks",
		"tls_mode",
		"total",
		"trigger",
		"user_id",
	}
	got := make([]string, 0, len(All))
	for _, f := range All {
		got = append(got, f.Key)
	}
	sort.Strings(got)
	if !slices.Equal(got, golden) {
		t.Fatalf("日志键面变化：\n got  = %q\n want = %q", got, golden)
	}
}
