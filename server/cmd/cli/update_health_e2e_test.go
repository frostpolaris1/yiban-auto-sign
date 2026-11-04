package main

// update.sh 健康检查失败的 e2e。
//
// # 功能
// 分支一：新二进制不健康、旧件健康 → 回滚成功，退出 1。
// 分支二：回滚后仍不健康 → 退出 2。
// 两个分支与「迁移失败」分支的差别：迁移已应用、二进制已换。
// 无签名调用点显式带 --allow-unsigned：默认强制验签（E-3）的逃生口。
//
// # 口径
// 假 systemctl 模拟 systemd 语义：单元 active 时 start 是 no-op，
// stop 清 active 后 start 才真正启动。假服务在启动时把当时的
// 可执行文件内容抄进「运行中」文件，健康检查只读该文件。
// 因此「回滚后守护进程跑的是 OLD」是可判的：
// 只换文件不重启时，运行中仍是 NEW。

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// fakeSystemd 写一个模拟 systemd 状态机的 fake systemctl。
// active 标记存在时 start 是 no-op；stop 删除 active 与运行中文件。
// 启动时把 FAKE_UNIT_BIN 的内容抄进 FAKE_RUNNING。
func fakeSystemd(t *testing.T, dir, logPath, unitBin, running, stateDir string) string {
	t.Helper()
	body := `echo "systemctl $*" >> "` + logPath + `"
case "$1" in
  start)
    if [ -f "` + stateDir + `/active" ]; then exit 0; fi
    : > "` + stateDir + `/active"
    cat "` + unitBin + `" > "` + running + `"
    ;;
  stop)
    rm -f "` + stateDir + `/active" "` + running + `"
    ;;
esac
exit 0
`
	return fakeBin(t, dir, "systemctl", body)
}

func TestUpdateShRollsBackOnFailedHealth(t *testing.T) {
	requireBash(t)
	root := t.TempDir()
	binDir := t.TempDir()
	sysctlLog := filepath.Join(t.TempDir(), "systemctl.log")
	stateDir := t.TempDir()
	oldBody := "echo OLD-SERVER\n"
	newBody := "echo NEW-SERVER\n"
	installed := filepath.Join(root, "usr/local/bin/yiban-server")
	if err := os.MkdirAll(filepath.Dir(installed), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(installed, []byte("#!/bin/sh\n"+oldBody), 0o755); err != nil {
		t.Fatalf("seed installed: %v", err)
	}
	// 更新前：单元已 active，守护进程跑的是 OLD。
	running := filepath.Join(stateDir, "proc")
	if err := os.WriteFile(filepath.Join(stateDir, "active"), nil, 0o644); err != nil {
		t.Fatalf("seed active: %v", err)
	}
	if err := os.WriteFile(running, []byte("#!/bin/sh\n"+oldBody), 0o644); err != nil {
		t.Fatalf("seed running: %v", err)
	}
	// 已装页面与 server.env：健康回滚必须连页面一起换回。
	oldUIDir := filepath.Join(root, "var/lib/yiban/ui")
	if err := os.MkdirAll(oldUIDir, 0o755); err != nil {
		t.Fatalf("mkdir old ui: %v", err)
	}
	if err := os.WriteFile(filepath.Join(oldUIDir, "index.html"), []byte("old-ui"), 0o644); err != nil {
		t.Fatalf("write old ui: %v", err)
	}
	if err := os.MkdirAll(filepath.Join(root, "etc/yiban"), 0o755); err != nil {
		t.Fatalf("mkdir etc: %v", err)
	}
	if err := os.WriteFile(filepath.Join(root, "etc/yiban/server.env"),
		[]byte("YIBAN_UI_DIR=/var/lib/yiban/ui\n"), 0o600); err != nil {
		t.Fatalf("write env: %v", err)
	}
	newUI := writeUIDistDir(t, "new-ui")
	newBin := fakeBin(t, binDir, "yiban-server-new", newBody)
	alwaysFail := filepath.Join(t.TempDir(), "health-fail")
	// 健康检查只看守护进程实际在跑的那份文件。
	cliBin := fakeBin(t, binDir, "yiban-cli", `
case "$1" in
  migrate) exit 0 ;;
  healthcheck)
    [ -f "$FAKE_HEALTH_FAIL" ] && exit 1
    grep -q OLD-SERVER "$FAKE_RUNNING" || exit 1
    exit 0
    ;;
esac
exit 0
`)
	sysctl := fakeSystemd(t, binDir, sysctlLog, installed, running, stateDir)
	// U2 起停服前有自检门与统一备份；健康失败后默认还会尝试数据库恢复。
	// psql 桩对迁移前后都报同一版本 ⇒ 恢复判定为 not-needed（本用例只测二进制/UI 回滚）。
	// 安装位 CLI 与服务用同一套探活判据：回滚会把 CLI 也换回旧件，
	// 之后的探活必须仍在「守护进程跑的是旧件」时成功、在 alwaysFail 时失败。
	fakeUpdateCLI(t, filepath.Join(root, "usr/local/bin"), "yiban-cli", `  migrate) exit 0 ;;
  healthcheck)
    [ -f "$FAKE_HEALTH_FAIL" ] && exit 1
    grep -q OLD-SERVER "$FAKE_RUNNING" || exit 1
    exit 0
    ;;`)
	fakePgTools(t, binDir)
	env := map[string]string{
		"YIBAN_INSTALL_ROOT":          root,
		"SYSTEMCTL":                   sysctl,
		"FAKE_HEALTH_FAIL":            alwaysFail,
		"FAKE_RUNNING":                running,
		"YIBAN_DATABASE_URL":          "postgres://yiban:pw@127.0.0.1:5432/yiban?sslmode=disable",
		"YIBAN_HEALTH_RETRIES":        "1",
		"YIBAN_HEALTH_RETRY_INTERVAL": "0",
		"PATH":                        binDir + string(os.PathListSeparator) + os.Getenv("PATH"),
	}

	// 分支一：新件不健康、旧件健康 → 回滚成功，退出 1。
	out, rc := runScript(t, scriptPath(t, "update.sh"), env,
		"--server-bin", newBin, "--cli-bin", cliBin,
		"--ui-dir", newUI, "--allow-unsigned")
	if rc != 1 {
		t.Fatalf("rollback-succeeded update rc=%d, want 1; out=%s", rc, out)
	}
	// 必须先走到回滚分支：否则「rc=1」可能来自脚本更早的失败。
	if !strings.Contains(out, "rolling back") {
		t.Fatalf("脚本未进入回滚分支（rc=%d）:\n%s", rc, out)
	}
	if body := readAll(t, installed); !strings.Contains(body, oldBody) {
		t.Fatalf("回滚后安装位应为旧件:\n%s", body)
	}
	// 判据：回滚必须真的重启守护进程，运行中必须是旧件。
	if body := readAll(t, running); !strings.Contains(body, oldBody) {
		t.Fatalf("回滚后守护进程仍在跑新件（未重启）:\n%s", body)
	}
	assertStopBeforeLastStart(t, readAll(t, sysctlLog))
	// 页面也必须回滚：旧二进制配新页面是新旧混装。
	if body := readAll(t, filepath.Join(oldUIDir, "index.html")); !strings.Contains(body, "old-ui") {
		t.Fatalf("回滚后页面应为旧件:\n%s", body)
	}
	if _, err := os.Stat(oldUIDir + ".bak"); err == nil {
		t.Fatalf("回滚后不应残留 %s.bak", oldUIDir)
	}

	// 分支二：强制健康恒失败 → 回滚后仍不健康，退出 2。
	if err := os.WriteFile(alwaysFail, []byte("x"), 0o644); err != nil {
		t.Fatalf("arm health fail: %v", err)
	}
	out, rc = runScript(t, scriptPath(t, "update.sh"), env,
		"--server-bin", newBin, "--cli-bin", cliBin,
		"--ui-dir", newUI, "--allow-unsigned")
	if rc != 2 {
		t.Fatalf("rollback-failed update rc=%d, want 2; out=%s", rc, out)
	}
}

// assertStopBeforeLastStart 断言 systemctl 调用序列里
// 最后一次 stop 出现在最后一次 start 之前：回滚先停服务再启动。
func assertStopBeforeLastStart(t *testing.T, log string) {
	t.Helper()
	lastStop, lastStart := strings.LastIndex(log, "stop yiban.service"), strings.LastIndex(log, "start yiban.service")
	if lastStop < 0 || lastStart < 0 {
		t.Fatalf("systemctl 序列缺 stop 或 start:\n%s", log)
	}
	if lastStop > lastStart {
		t.Fatalf("回滚必须在 start 之前 stop（否则 start 对 active 单元是 no-op）:\n%s", log)
	}
	if n := strings.Count(log, "stop yiban.service"); n != 2 {
		t.Fatalf("stop 次数 = %d，want 2（更新前一次、回滚再停一次）:\n%s", n, log)
	}
}
