package app

// 本文件定义三个用例域共用的分页口径。
//
// 归属：app 层共用取值。入口层把查询串的 limit 解成指针后交给本函数，
// 用例与仓储都只接收已定型的 1..200 整数。
//
// 口径：缺省 50；上限 200。0 表示「调用方没给」，
// 除缺省以外的越界一律校验失败，**不静默夹到边界**。

import (
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

const (
	// DefaultPageSize 是未给 limit 时的页大小。
	DefaultPageSize = 50
	// MaxPageSize 是允许的最大页大小。
	MaxPageSize = 200
)

// RequireScope 拒绝零值作用域。
// 零值说明调用方漏传或构造错误：**运行期报 internal_error**，
// 不把它当成「没有任何院校」。
func RequireScope(scope corescope.Scope) error {
	if !scope.Valid() {
		return errs.New(errs.CodeInternal, "invalid scope")
	}
	return nil
}

// PageLimit 把查询串的可选 limit 定型为 1..200。
// nil 返回 DefaultPageSize；**显式 0 是校验失败**，与「没给」区分。
func PageLimit(limit *int) (int, error) {
	if limit == nil {
		return DefaultPageSize, nil
	}
	if *limit < 1 || *limit > MaxPageSize {
		return 0, errs.New(errs.CodeValidation, "limit must be between 1 and 200")
	}
	return *limit, nil
}

// NormalizeLimit 校验用例层收到的 limit。
// 0 仍按缺省处理，供内部调用方省略页大小时使用。
func NormalizeLimit(limit int) (int, error) {
	if limit == 0 {
		return DefaultPageSize, nil
	}
	if limit < 1 || limit > MaxPageSize {
		return 0, errs.New(errs.CodeValidation, "limit must be between 1 and 200")
	}
	return limit, nil
}
