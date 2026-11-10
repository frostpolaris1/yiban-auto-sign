package pg_test

import "context"

// fixedQuota 是测试用固定配额替身：0=不限，正数=上限。
// 本包真库 e2e 显式注入上限；配额写回链路由 quota_e2e_test.go 用
// 真实 appsettings.Resolver 覆盖。
type fixedQuota int

func (q fixedQuota) UserLimit(context.Context) (int, error)    { return int(q), nil }
func (q fixedQuota) AccountLimit(context.Context) (int, error) { return int(q), nil }
