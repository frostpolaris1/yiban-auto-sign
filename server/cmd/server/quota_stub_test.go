package main

import "context"

// fixedQuota 是测试用固定配额替身：0=不限，正数=上限。
// 走 assembly 的 e2e 用真实覆盖链（缺行=1/1，夹具按需抬配额）；
// 本替身只服务直接构造 app 用例的测试。
type fixedQuota int

func (q fixedQuota) UserLimit(context.Context) (int, error)    { return int(q), nil }
func (q fixedQuota) AccountLimit(context.Context) (int, error) { return int(q), nil }
