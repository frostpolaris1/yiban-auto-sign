package main

// usage 清单的完整性（派单 F14）。
//
// # 功能
// help 输出必须登记 main 的每个子命令。user 子命令缺失时，
// 运维不知道 create-master 存在（干净库无人能登录）。
//
// # 口径
// 按行首缩进匹配命令名：避免 "server" 出现在说明文字里也算命中。

import (
	"regexp"
	"strings"
	"testing"
)

func TestUsageListsEverySubcommand(t *testing.T) {
	var b strings.Builder
	usage(&b)
	out := b.String()
	for _, name := range []string{
		"server", "migrate", "doctor", "healthcheck", "backup", "restore", "user", "help",
		"backfill-time-prefs",
	} {
		re := regexp.MustCompile(`(?m)^\s{2}` + regexp.QuoteMeta(name) + `\b`)
		if !re.MatchString(out) {
			t.Fatalf("usage 缺子命令 %q：\n%s", name, out)
		}
	}
	if !strings.Contains(out, "user create-master") {
		t.Fatalf("usage 必须点名 create-master（部署最后一步）：\n%s", out)
	}
}
