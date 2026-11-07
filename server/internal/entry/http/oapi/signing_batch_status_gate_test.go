package oapi_test

// 批量签到项状态枚举的同集守卫。
//
// # 功能
// 钉住契约 AccountBatchSignItemStatus 的生成值与 signing 处理器写入的
// 两个状态逐值一致，且两侧都不多出第三个值。枚举豁免表只挡覆盖门；
// 本用例才是同集判据：任一侧新增、删除或改值即红。
//
// # 口径
// 常量从源文件读：oapi.gen.go 是生成物，signing 侧词表是未标注类型的
// 字符串常量（BatchSignItemStatus 前缀），故 signing 侧按前缀口径单独收集。
// entry→entry 是同层引用，本文件只做词表对账，不引行为。
// 形状照 user_batch_action_gate_test.go（用户批量），两处互不引用。

import (
	"go/ast"
	"go/token"
	"strconv"
	"strings"
	"testing"

	oapi "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	signinghttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/signing"
)

// TestBatchSignItemStatusEnumMatchesSigningConstants：契约生成值与处理器常量逐值一致。
func TestBatchSignItemStatusEnumMatchesSigningConstants(t *testing.T) {
	pairs := []struct {
		api    oapi.AccountBatchSignItemStatus
		status string
	}{
		{oapi.AccountBatchSignItemStatusOk, signinghttp.BatchSignItemStatusOK},
		{oapi.AccountBatchSignItemStatusError, signinghttp.BatchSignItemStatusError},
	}
	if len(pairs) != 2 {
		t.Fatalf("pairs = %d, want 2", len(pairs))
	}
	for _, pair := range pairs {
		if string(pair.api) != pair.status {
			t.Errorf("contract value %q != signing constant %q", pair.api, pair.status)
		}
	}

	// 双向同集：契约侧或 signing 侧多出未配对的值也红。
	apiValues := sourceEnumValues(t, ".", "AccountBatchSignItemStatus", "AccountBatchSignItemStatus", 2)
	signingValues := batchSigningStatusValues(t)
	if got, want := sortedJoin(apiValues), sortedJoin(signingValues); got != want {
		t.Fatalf("contract enum values = %q, want signing constants %q", got, want)
	}
}

// batchSigningStatusValues 从 signing 源码读 BatchSignItemStatus 前缀的无类型字符串常量。
// signing 侧词表未标注类型，走不了 sourceEnumValues 的显式类型口径。
func batchSigningStatusValues(t *testing.T) []string {
	t.Helper()
	var out []string
	walkConsts(t, "../../../entry/http/signing", func(value *ast.ValueSpec) {
		if value.Type != nil {
			return
		}
		for _, name := range value.Names {
			if !strings.HasPrefix(name.Name, "BatchSignItemStatus") {
				continue
			}
			for _, expr := range value.Values {
				lit, ok := expr.(*ast.BasicLit)
				if !ok || lit.Kind != token.STRING {
					t.Fatalf("signing: BatchSignItemStatus const %s is not a string literal", name.Name)
				}
				text, err := strconv.Unquote(lit.Value)
				if err != nil {
					t.Fatalf("unquote %s: %v", name.Name, err)
				}
				out = append(out, text)
			}
		}
	})
	if len(out) != 2 {
		t.Fatalf("signing BatchSignItemStatus* string constants = %d (%v), want 2", len(out), out)
	}
	return out
}
