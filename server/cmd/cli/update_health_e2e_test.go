package main

// update.sh 健康检查失败的 e2e（P7 审查 P3-9）。
//
// # 功能
// 分支一：新二进制不健康、旧件健康 → 回滚成功，退出 1。
// 分支二：回滚后仍不健康 → 退出 2。
// 两个分支与「迁移失败」分支的差别：迁移已应用、二进制已换。

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestUpdateShRollsBackOnFailedHealth(t *testing.T) {
	requireBash(t)
	root := t.TempDir()
	sysctlLog := filepath.Join(t.TempDir(), "systemctl.log")
	binDir := t.TempDir()
	oldBody := "echo OLD-SERVER\n"
	newBody := "echo NEW-SERVER\n"
	installed := filepath.Join(root, "usr/local/bin/yiban-server")
	if err := os.MkdirAll(filepath.Dir(installed), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(installed, []byte("#!/bin/sh\n"+oldBody), 0o755); err != nil {
		t.Fatalf("seed installed: %v", err)
	}
	newBin := fakeBin(t, binDir, "yiban-server-new", newBody)
	alwaysFail := filepath.Join(t.TempDir(), "health-fail")
	cliBin := fakeBin(t, binDir, "yiban-cli", `
case "$1" in
  migrate) exit 0 ;;
  healthcheck)
    [ -f "$FAKE_HEALTH_FAIL" ] && exit 1
    grep -q OLD-SERVER "$YIBAN_INSTALL_ROOT/usr/local/bin/yiban-server" || exit 1
    exit 0
    ;;
esac
exit 0
`)
	sysctl := fakeBin(t, binDir, "systemctl",
		"echo \"systemctl $*\" >> \""+sysctlLog+"\"\nexit 0\n")
	env := map[string]string{
		"YIBAN_INSTALL_ROOT":          root,
		"SYSTEMCTL":                   sysctl,
		"FAKE_HEALTH_FAIL":            alwaysFail,
		"YIBAN_HEALTH_RETRIES":        "1",
		"YIBAN_HEALTH_RETRY_INTERVAL": "0",
		"PATH":                        binDir + string(os.PathListSeparator) + os.Getenv("PATH"),
	}

	// 分支一：新件不健康、旧件健康 → 回滚成功，退出 1。
	out, rc := runScript(t, scriptPath(t, "update.sh"), env,
		"--server-bin", newBin, "--cli-bin", cliBin)
	if rc != 1 {
		t.Fatalf("rollback-succeeded update rc=%d, want 1; out=%s", rc, out)
	}
	if body := readAll(t, installed); !strings.Contains(body, oldBody) {
		t.Fatalf("回滚后安装位应为旧件:\n%s", body)
	}
	if log := readAll(t, sysctlLog); !strings.Contains(log, "start") {
		t.Fatalf("回滚必须重启服务:\n%s", log)
	}

	// 分支二：强制健康恒失败 → 回滚后仍不健康，退出 2。
	if err := os.WriteFile(alwaysFail, []byte("x"), 0o644); err != nil {
		t.Fatalf("arm health fail: %v", err)
	}
	out, rc = runScript(t, scriptPath(t, "update.sh"), env,
		"--server-bin", newBin, "--cli-bin", cliBin)
	if rc != 2 {
		t.Fatalf("rollback-failed update rc=%d, want 2; out=%s", rc, out)
	}
}
