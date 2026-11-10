package main

import "context"

// fixedQuota 是 CLI 测试用固定配额替身：0=不限，正数=上限。
// create-master 的生产装配走 assembly.NewSettingsResolver（真覆盖链）；
// 本替身只服务直接构造 app/auth 用例的 e2e。
type fixedQuota int

func (q fixedQuota) UserLimit(context.Context) (int, error)    { return int(q), nil }
func (q fixedQuota) AccountLimit(context.Context) (int, error) { return int(q), nil }
