package account

import "context"

// fixedQuota 是测试用固定配额替身：0=不限，正数=上限。
// 生产实现是 app/settings.Resolver（DB 覆盖链）；本包测试只关心
// 用例把读到的值原样交给仓储。
type fixedQuota int

func (q fixedQuota) UserLimit(context.Context) (int, error)    { return int(q), nil }
func (q fixedQuota) AccountLimit(context.Context) (int, error) { return int(q), nil }
