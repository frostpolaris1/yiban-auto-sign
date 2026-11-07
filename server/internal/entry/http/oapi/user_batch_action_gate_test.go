package oapi_test

// 用户批量动作枚举的同集守卫。
//
// # 功能
// 钉住契约 UserBatchAction 的生成值与 app/user 批量动作常量
// 逐值一致，且两侧都不多出第六个值。枚举豁免表只挡覆盖门；
// 本用例才是同集判据：任一侧新增、删除或改值即红。
//
// # 口径
// 常量从源文件读：oapi.gen.go 是生成物，app 侧词表是未标注类型的
// 字符串常量（Batch 前缀），故 app 侧按前缀口径单独收集。
// entry→app 是允许的分层方向；本文件只做词表对账，不引行为。
// 形状照 batch_action_gate_test.go（账号批量），两处互不引用。

import (
	"go/ast"
	"go/token"
	"strconv"
	"strings"
	"testing"

	appuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/user"
	oapi "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
)

// TestUserBatchActionEnumMatchesAppConstants：契约生成值与 app 常量逐值一致。
func TestUserBatchActionEnumMatchesAppConstants(t *testing.T) {
	pairs := []struct {
		api    oapi.UserBatchAction
		action string
	}{
		{oapi.UserBatchActionDisable, appuser.BatchDisable},
		{oapi.UserBatchActionEnable, appuser.BatchEnable},
		{oapi.UserBatchActionResetPassword, appuser.BatchResetPassword},
		{oapi.UserBatchActionDelete, appuser.BatchDelete},
		{oapi.UserBatchActionRestore, appuser.BatchRestore},
	}
	if len(pairs) != 5 {
		t.Fatalf("pairs = %d, want 5", len(pairs))
	}
	for _, pair := range pairs {
		if string(pair.api) != pair.action {
			t.Errorf("contract value %q != app constant %q", pair.api, pair.action)
		}
	}

	// 双向同集：契约侧或 app 侧多出未配对的值也红。
	apiValues := sourceEnumValues(t, ".", "UserBatchAction", "UserBatchAction", 5)
	appValues := batchAppUserActionValues(t)
	if got, want := sortedJoin(apiValues), sortedJoin(appValues); got != want {
		t.Fatalf("contract enum values = %q, want app constants %q", got, want)
	}
}

// batchAppUserActionValues 从 app/user 源码读 Batch 前缀的无类型字符串常量。
// app 侧词表未标注类型，走不了 sourceEnumValues 的显式类型口径。
func batchAppUserActionValues(t *testing.T) []string {
	t.Helper()
	var out []string
	walkConsts(t, "../../../app/user", func(value *ast.ValueSpec) {
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
					t.Fatalf("app/user: Batch const %s is not a string literal", name.Name)
				}
				text, err := strconv.Unquote(lit.Value)
				if err != nil {
					t.Fatalf("unquote %s: %v", name.Name, err)
				}
				out = append(out, text)
			}
		}
	})
	if len(out) != 5 {
		t.Fatalf("app/user Batch* string constants = %d (%v), want 5", len(out), out)
	}
	return out
}
