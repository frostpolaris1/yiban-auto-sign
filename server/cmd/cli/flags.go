// 子命令共用的 FlagSet 构造。
package main

import (
	"flag"
	"io"
)

// newFlagSet 构造 ContinueOnError 的 FlagSet，错误输出定向到 out。
// 各子命令在 Parse 失败时自行返回退出码 2。
func newFlagSet(name string, out io.Writer) *flag.FlagSet {
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	fs.SetOutput(out)
	return fs
}
