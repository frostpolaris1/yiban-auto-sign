package app

// 分页口径的边界用例。
//
// 本文件**不**连库、**不**测路由；只钉住用例层共用的取值口径。

import (
	"testing"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// 零值作用域是调用方构造错误，不是「没有任何院校」。
// 它必须报 internal_error，且不得被当成合法的空集合。
func TestRequireScopeRejectsZeroValue(t *testing.T) {
	err := RequireScope(corescope.Scope{})
	if err == nil {
		t.Fatal("RequireScope accepted the zero value")
	}
	if code := errs.From(err).Code; code != errs.CodeInternal {
		t.Fatalf("code = %q, want internal_error", code)
	}
}

// 三种合法形状都必须通过。
func TestRequireScopeAcceptsValidShapes(t *testing.T) {
	for name, scope := range map[string]corescope.Scope{
		"master": corescope.Master(),
		"admin":  corescope.Admin([]string{"018f0000-0000-7000-8000-000000000001"}),
		"user":   corescope.User("018f0000-0000-7000-8000-000000000002"),
	} {
		if err := RequireScope(scope); err != nil {
			t.Fatalf("%s scope: %v", name, err)
		}
	}
}
