package schedule

import "errors"

// ErrWindowStale 是窗口版本竞争失败：库里已有更新版本。
// 定义在 core 层：pg 适配器与 app 用例按同一哨兵判定，不各造一份。
var ErrWindowStale = errors.New("schedule: window version conflict")
