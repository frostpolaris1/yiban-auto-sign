package user

import "context"

// fixedQuota 是测试用固定配额替身：0=不限，正数=上限。
type fixedQuota int

func (q fixedQuota) UserLimit(context.Context) (int, error)    { return int(q), nil }
func (q fixedQuota) AccountLimit(context.Context) (int, error) { return int(q), nil }
