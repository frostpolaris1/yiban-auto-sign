package oapi_test

// 批量动作枚举的同集守卫。
//
// # 功能
// 钉住契约 AccountBatchAction 的生成值与 app/account 批量动作常量
// 逐值一致，且两侧都不多出第七个值。枚举豁免表只挡覆盖门；
// 本用例才是同集判据：任一侧新增、删除或改值即红。
//
// # 口径
// 常量从源文件读：oapi.gen.go 是生成物，app 侧词表是未标注类型的
// 字符串常量（Batch 前缀），故 app 侧按前缀口径单独收集。
// entry→app 是允许的分层方向；本文件只做词表对账，不引行为。

import (
	"go/ast"
	"go/token"
	"strconv"
	"strings"
	"testing"

	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	oapi "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
)

// TestBatchActionEnumMatchesAppConstants：契约生成值与 app 常量逐值一致。
func TestBatchActionEnumMatchesAppConstants(t *testing.T) {
	pairs := []struct {
		api    oapi.AccountBatchAction
		action string
	}{
		{oapi.ReviewApprove, appaccount.BatchReviewApprove},
		{oapi.ReviewReject, appaccount.BatchReviewReject},
		{oapi.Pause, appaccount.BatchPause},
		{oapi.Resume, appaccount.BatchResume},
		{oapi.Delete, appaccount.BatchDelete},
		{oapi.Restore, appaccount.BatchRestore},
	}
	if len(pairs) != 6 {
		t.Fatalf("pairs = %d, want 6", len(pairs))
	}
	for _, pair := range pairs {
		if string(pair.api) != pair.action {
			t.Errorf("contract value %q != app constant %q", pair.api, pair.action)
		}
	}

	// 双向同集：契约侧或 app 侧多出未配对的值也红。
	apiValues := sourceEnumValues(t, ".", "AccountBatchAction", "AccountBatchAction", 6)
	appValues := batchAppActionValues(t)
	if got, want := sortedJoin(apiValues), sortedJoin(appValues); got != want {
		t.Fatalf("contract enum values = %q, want app constants %q", got, want)
	}
}

// batchAppActionValues 从 app/account 源码读 Batch 前缀的无类型字符串常量。
// app 侧词表未标注类型，走不了 sourceEnumValues 的显式类型口径。
func batchAppActionValues(t *testing.T) []string {
	t.Helper()
	var out []string
	walkConsts(t, "../../../app/account", func(value *ast.ValueSpec) {
		if value.Type != nil {
			return
		}
		for _, name := range value.Names {
			if !strings.HasPrefix(name.Name, "Batch") {
				continue
			}
			for _, expr := range value.Values {
				lit, ok := expr.(*ast.BasicLit)
				if !ok || lit.Kind != token.STRING {
					t.Fatalf("app/account: Batch const %s is not a string literal", name.Name)
				}
				text, err := strconv.Unquote(lit.Value)
				if err != nil {
					t.Fatalf("unquote %s: %v", name.Name, err)
				}
				out = append(out, text)
			}
		}
	})
	if len(out) != 6 {
		t.Fatalf("app/account Batch* string constants = %d (%v), want 6", len(out), out)
	}
	return out
}
