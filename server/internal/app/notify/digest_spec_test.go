package notify

// 摘要触发时刻的钉住判据（P2-1）。
//
// digestSpec 是 unexported 常量，行为测试只覆盖启动补发；若没有这道门，
// 把 09:00 改成其它时刻不会让任何测试变红。本文件在包内直接读常量。

import "testing"

// TestDigestSpecPinned 钉住业务日 09:00 的 cron spec。
func TestDigestSpecPinned(t *testing.T) {
	if digestSpec != "0 9 * * *" {
		t.Fatalf("digestSpec = %q, want %q", digestSpec, "0 9 * * *")
	}
}
