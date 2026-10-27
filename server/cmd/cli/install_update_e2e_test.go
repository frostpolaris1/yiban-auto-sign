package main

// install.sh / update.sh 的干跑 e2e（派单 P7 判据）。
//
// # 功能
// 假根目录（YIBAN_INSTALL_ROOT）+ 假 systemctl（SYSTEMCTL）+
// 假二进制：断言文件树落位、systemd 调用序列、健康检查重试、
// 以及 update 的迁移失败回滚分支。
// 本文件**不**装真服务、**不**碰真 /etc、**不**要求 root。
//
// # 口径
// Windows 无 bash 与 POSIX 文件语义，整文件跳过并打印原因。

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// scriptPath 返回 deploy/linux 下脚本的绝对路径。
func scriptPath(t *testing.T, name string) string {
	t.Helper()
	p, err := filepath.Abs(filepath.Join("..", "..", "..", "deploy", "linux", name))
	if err != nil {
		t.Fatalf("resolve script: %v", err)
	}
	return p
}

// requireBash 在非 unix 平台显性跳过。
func requireBash(t *testing.T) {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("SKIP: Windows 没有 bash 与 POSIX 文件语义，干跑 e2e 无法构造")
	}
}

// runScript 执行 bash 脚本并返回 stdout+stderr 与退出码。
// env 追加在进程环境之后：同名键以后写的为准。
func runScript(t *testing.T, script string, env map[string]string, args ...string) (string, int) {
	t.Helper()
	cmd := exec.Command("bash", append([]string{script}, args...)...)
	cmd.Env = os.Environ()
	for k, v := range env {
		cmd.Env = append(cmd.Env, k+"="+v)
	}
	var buf bytes.Buffer
	cmd.Stdout = &buf
	cmd.Stderr = &buf
	err := cmd.Run()
	rc := 0
	if exit, ok := err.(*exec.ExitError); ok {
		rc = exit.ExitCode()
	} else if err != nil {
		t.Fatalf("run %s: %v", script, err)
	}
	return buf.String(), rc
}

// fakeBin 写一个可执行的 shell 脚本当假二进制。
func fakeBin(t *testing.T, dir, name, body string) string {
	t.Helper()
	p := filepath.Join(dir, name)
	if err := os.WriteFile(p, []byte("#!/bin/sh\n"+body), 0o755); err != nil {
		t.Fatalf("write fake bin %s: %v", name, err)
	}
	return p
}

// readAll 读文件树断言用的文件内容。
func readAll(t *testing.T, path string) string {
	t.Helper()
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	return string(body)
}

func TestInstallShDryRun(t *testing.T) {
	requireBash(t)
	root := t.TempDir()
	sysctlLog := filepath.Join(t.TempDir(), "systemctl.log")
	binDir := t.TempDir()
	serverBin := fakeBin(t, binDir, "yiban-server", "while :; do sleep 1; done")
	cliBin := fakeBin(t, binDir, "yiban-cli", `
case "$1" in
  healthcheck) [ -f "$FAKE_HEALTH_OK" ] && exit 0 || exit 1 ;;
  migrate) echo "schema version 16" ;;
esac
exit 0
`)
	healthFlag := filepath.Join(t.TempDir(), "health-ok")

	env := map[string]string{
		"YIBAN_INSTALL_ROOT": root,
		"SYSTEMCTL": fakeBin(t, binDir, "systemctl",
			"echo \"systemctl $*\" >> \""+sysctlLog+"\"\nexit 0\n"),
		"YIBAN_DATABASE_URL":          "postgres://yiban:pw@127.0.0.1:5432/yiban?sslmode=disable",
		"FAKE_HEALTH_OK":              healthFlag,
		"PATH":                        binDir + string(os.PathListSeparator) + os.Getenv("PATH"),
		"YIBAN_HEALTH_RETRIES":        "3",
		"YIBAN_HEALTH_RETRY_INTERVAL": "0",
	}

	// 第一次：健康检查不打标 → 安装应重试后失败（退出非 0）但文件树已落位。
	out, rc := runScript(t, scriptPath(t, "install.sh"), env,
		"--server-bin", serverBin, "--cli-bin", cliBin)
	if rc == 0 {
		t.Fatalf("healthcheck not reachable, install must fail; out=%s", out)
	}

	// 文件树断言。
	for _, p := range []string{
		filepath.Join(root, "usr/local/bin/yiban-server"),
		filepath.Join(root, "usr/local/bin/yiban-cli"),
		filepath.Join(root, "etc/yiban/.env"),
		filepath.Join(root, "etc/systemd/system/yiban.service"),
	} {
		if _, err := os.Stat(p); err != nil {
			t.Fatalf("install tree missing %s: %v\nout=%s", p, err, out)
		}
	}
	envBody := readAll(t, filepath.Join(root, "etc/yiban/.env"))
	for _, key := range []string{"YIBAN_ENV=", "YIBAN_HASH_PEPPER=", "YIBAN_CREDENTIAL_KEY=", "YIBAN_DATABASE_URL="} {
		if !strings.Contains(envBody, key) {
			t.Fatalf(".env missing %s:\n%s", key, envBody)
		}
	}
	unit := readAll(t, filepath.Join(root, "etc/systemd/system/yiban.service"))
	if !strings.Contains(unit, "ExecStart=/usr/local/bin/yiban-server") ||
		!strings.Contains(unit, "User=yiban") ||
		!strings.Contains(unit, "EnvironmentFile=/etc/yiban/.env") {
		t.Fatalf("unit file shape wrong:\n%s", unit)
	}
	log := readAll(t, sysctlLog)
	if !strings.Contains(log, "daemon-reload") || !strings.Contains(log, "enable --now") {
		t.Fatalf("systemctl sequence missing steps:\n%s", log)
	}

	// 第二次：健康检查放行 → 安装应走完并退出 0。
	touchHealthFlag(t, healthFlag)
	out, rc = runScript(t, scriptPath(t, "install.sh"), env,
		"--server-bin", serverBin, "--cli-bin", cliBin)
	if rc != 0 {
		t.Fatalf("install with healthy probe: rc=%d out=%s", rc, out)
	}
	// 部署的最后一步必须被点名：干净库还没有可登录账号
	//（P7 审查 P2-1）。
	if !strings.Contains(out, "user create-master") {
		t.Fatalf("install must print the create-master next step; out=%s", out)
	}
}

func touchHealthFlag(t *testing.T, path string) {
	t.Helper()
	if err := os.WriteFile(path, []byte("ok"), 0o644); err != nil {
		t.Fatalf("touch health flag: %v", err)
	}
}

func TestInstallShRejectsMissingBinary(t *testing.T) {
	requireBash(t)
	root := t.TempDir()
	cliBin := fakeBin(t, t.TempDir(), "yiban-cli", "exit 0\n")
	out, rc := runScript(t, scriptPath(t, "install.sh"),
		map[string]string{
			"YIBAN_INSTALL_ROOT": root,
			"SYSTEMCTL":          fakeBin(t, t.TempDir(), "systemctl", "exit 0\n"),
			"YIBAN_DATABASE_URL": "postgres://yiban:pw@127.0.0.1:5432/yiban",
		},
		"--server-bin", "/no/such/yiban-server", "--cli-bin", cliBin)
	if rc == 0 {
		t.Fatalf("missing server binary must fail; out=%s", out)
	}
	if !strings.Contains(out, "yiban-server") {
		t.Fatalf("error must name the missing binary: %s", out)
	}
}

func TestUpdateShRollsBackOnFailedMigration(t *testing.T) {
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
	// 假 cli：migrate 在打标文件存在时失败；healthcheck 成功。
	migrateFail := filepath.Join(t.TempDir(), "migrate-fail")
	cliBin := fakeBin(t, binDir, "yiban-cli", `
case "$1" in
  migrate) [ -f "$FAKE_MIGRATE_FAIL" ] && exit 1; exit 0 ;;
  healthcheck) exit 0 ;;
esac
exit 0
`)
	env := map[string]string{
		"YIBAN_INSTALL_ROOT": root,
		"SYSTEMCTL": fakeBin(t, binDir, "systemctl",
			"echo \"systemctl $*\" >> \""+sysctlLog+"\"\nexit 0\n"),
		"FAKE_MIGRATE_FAIL": migrateFail,
		"PATH":              binDir + string(os.PathListSeparator) + os.Getenv("PATH"),
	}
	if err := os.WriteFile(migrateFail, []byte("x"), 0o644); err != nil {
		t.Fatalf("arm migrate fail: %v", err)
	}

	out, rc := runScript(t, scriptPath(t, "update.sh"), env,
		"--server-bin", newBin, "--cli-bin", cliBin)
	if rc == 0 {
		t.Fatalf("failed migration must fail the update; out=%s", out)
	}
	// 回滚断言：安装位仍是旧二进制。
	if body := readAll(t, installed); !strings.Contains(body, oldBody) {
		t.Fatalf("installed binary must be rolled back to old:\n%s", body)
	}
	log := readAll(t, sysctlLog)
	if !strings.Contains(log, "stop") || !strings.Contains(log, "start") {
		t.Fatalf("rollback must stop then start the service:\n%s", log)
	}

	// 成功分支：撤掉失败标记 → 迁移通过 → 新二进制就位，旧件留 .bak。
	if err := os.Remove(migrateFail); err != nil {
		t.Fatalf("disarm migrate fail: %v", err)
	}
	out, rc = runScript(t, scriptPath(t, "update.sh"), env,
		"--server-bin", newBin, "--cli-bin", cliBin)
	if rc != 0 {
		t.Fatalf("successful update: rc=%d out=%s", rc, out)
	}
	if body := readAll(t, installed); !strings.Contains(body, newBody) {
		t.Fatalf("installed binary must be the new one:\n%s", body)
	}
	bak := installed + ".bak"
	if body := readAll(t, bak); !strings.Contains(body, oldBody) {
		t.Fatalf("old binary must be kept at %s:\n%s", bak, body)
	}
}
