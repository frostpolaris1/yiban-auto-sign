package main

// update.sh 载入 /etc/yiban/.env 的 e2e（派单 F3）。
//
// # 功能
// 模拟真实运维动作：管理员把含特殊字符的 DSN 写进 .env，
// 再跑 update.sh 升级。迁移进程必须收到完整 DSN。
// 特殊字符：`&` 与 `#` —— 用 shell source 载入时，
// `&` 把赋值切成后台子 shell，变量不外泄，迁移拿到空 DSN。
// 无签名调用点显式带 --allow-unsigned：默认强制验签（E-3）的逃生口。
//
// # 口径
// 断言假 yiban-cli 的 migrate 收到的 YIBAN_DATABASE_URL 与 .env 原文一致。
// 干跑前缀：不碰真 /etc、不需要 root。

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestUpdateShLoadsEnvWithoutShellExpansion(t *testing.T) {
	requireBash(t)
	root := t.TempDir()
	binDir := t.TempDir()
	sysctlLog := filepath.Join(t.TempDir(), "systemctl.log")
	stateDir := t.TempDir()

	installed := filepath.Join(root, "usr/local/bin/yiban-server")
	if err := os.MkdirAll(filepath.Dir(installed), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(installed, []byte("#!/bin/sh\necho OLD-SERVER\n"), 0o755); err != nil {
		t.Fatalf("seed installed: %v", err)
	}
	running := filepath.Join(stateDir, "proc")
	if err := os.WriteFile(filepath.Join(stateDir, "active"), nil, 0o644); err != nil {
		t.Fatalf("seed active: %v", err)
	}
	if err := os.WriteFile(running, []byte("#!/bin/sh\necho OLD-SERVER\n"), 0o644); err != nil {
		t.Fatalf("seed running: %v", err)
	}
	sysctl := fakeSystemd(t, binDir, sysctlLog, installed, running, stateDir)

	// .env 里放三种形状：注释行、含 & 与 # 的 DSN、成对引号值。
	envDir := filepath.Join(root, "etc/yiban")
	if err := os.MkdirAll(envDir, 0o755); err != nil {
		t.Fatalf("mkdir env dir: %v", err)
	}
	const dsn = "postgres://yiban:p&a#ss@127.0.0.1:5432/yiban?sslmode=disable"
	envBody := "# 部署密钥，勿外传\n" +
		"\n" +
		"YIBAN_DATABASE_URL=" + dsn + "\n" +
		"YIBAN_HASH_PEPPER='0123456789abcdef'\n"
	if err := os.WriteFile(filepath.Join(envDir, ".env"), []byte(envBody), 0o600); err != nil {
		t.Fatalf("write .env: %v", err)
	}

	dsnOut := filepath.Join(t.TempDir(), "dsn.txt")
	pepperOut := filepath.Join(t.TempDir(), "pepper.txt")
	cliBin := fakeBin(t, binDir, "yiban-cli", `
case "$1" in
  migrate)
    printf '%s' "$YIBAN_DATABASE_URL" > "$FAKE_DSN_OUT"
    printf '%s' "$YIBAN_HASH_PEPPER" > "$FAKE_PEPPER_OUT"
    exit 0
    ;;
  healthcheck) exit 0 ;;
esac
exit 0
`)
	// 有 DSN 时 update.sh 要求迁移前快照：装个成功的 pg_dump 桩，
	// 本用例关心的是 .env 原文（含 & 与 #）能原样交给 cli。
	fakePgDump(t, binDir)
	env := map[string]string{
		"YIBAN_INSTALL_ROOT":          root,
		"SYSTEMCTL":                   sysctl,
		"FAKE_DSN_OUT":                dsnOut,
		"FAKE_PEPPER_OUT":             pepperOut,
		"YIBAN_HEALTH_RETRIES":        "1",
		"YIBAN_HEALTH_RETRY_INTERVAL": "0",
		"PATH":                        binDir + string(os.PathListSeparator) + os.Getenv("PATH"),
	}
	newBin := fakeBin(t, binDir, "yiban-server-new", "echo NEW-SERVER\n")

	out, rc := runScript(t, scriptPath(t, "update.sh"), env,
		"--server-bin", newBin, "--cli-bin", cliBin, "--allow-unsigned")
	if rc != 0 {
		t.Fatalf("update rc=%d, want 0; out=%s", rc, out)
	}
	if got := readAll(t, dsnOut); got != dsn {
		t.Fatalf("迁移收到的 DSN = %q，want %q（.env 不能被 shell 展开）", got, dsn)
	}
	if got := readAll(t, pepperOut); got != "0123456789abcdef" {
		t.Fatalf("成对单引号必须去掉：pepper = %q", got)
	}
	// 名册随升级一起落位：与仓库内那份逐字节一致（派单 F8 同族）。
	wantReg, err := os.ReadFile(filepath.Join("..", "..", "..", "config", "registry.json"))
	if err != nil {
		t.Fatalf("read repo registry: %v", err)
	}
	if got := []byte(readAll(t, filepath.Join(root, "etc/yiban/registry.json"))); !bytes.Equal(got, wantReg) {
		t.Fatalf("升级后的名册与 config/registry.json 不一致（%d vs %d 字节）", len(got), len(wantReg))
	}
	if strings.Contains(out, "command not found") {
		t.Fatalf("脚本里出现 command not found：.env 被当脚本执行了\n%s", out)
	}
}
