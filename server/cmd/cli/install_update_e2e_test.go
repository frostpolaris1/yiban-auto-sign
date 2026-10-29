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
	"crypto/sha256"
	"encoding/hex"
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
		filepath.Join(root, "etc/yiban/registry.json"),
		filepath.Join(root, "etc/systemd/system/yiban.service"),
	} {
		if _, err := os.Stat(p); err != nil {
			t.Fatalf("install tree missing %s: %v\nout=%s", p, err, out)
		}
	}
	// 名册必须与仓库内那的一份逐字节一致：doctor 的未登记键体检靠它。
	wantReg, err := os.ReadFile(filepath.Join("..", "..", "..", "config", "registry.json"))
	if err != nil {
		t.Fatalf("read repo registry: %v", err)
	}
	if got := []byte(readAll(t, filepath.Join(root, "etc/yiban/registry.json"))); !bytes.Equal(got, wantReg) {
		t.Fatalf("安装的名册与 config/registry.json 不一致（%d vs %d 字节）", len(got), len(wantReg))
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
	// 提示必须给出环境载入方式：.env 是 0600 root-only，
	// 新开的 shell 里 YIBAN_* 未设置，直接跑 create-master 会报缺 DSN（派单 F15）。
	if !strings.Contains(out, "set -a; . /etc/yiban/.env; set +a") {
		t.Fatalf("install must print how to load /etc/yiban/.env before create-master; out=%s", out)
	}
	// 口令只走管道：推荐用法不得把口令、DSN、pepper 写进 argv（派单 P1-3）。
	if !strings.Contains(out, "--password-stdin") {
		t.Fatalf("install must recommend --password-stdin; out=%s", out)
	}
	for _, bad := range []string{"--password <", "--dsn <", "--pepper <"} {
		if strings.Contains(out, bad) {
			t.Fatalf("install must not recommend argv secrets (%q); out=%s", bad, out)
		}
	}
	// 安装单元必须打印一条 create-master 命令：判定是「有管道 + 子命令」同一行。
	if !strings.Contains(out, "| yiban-cli user create-master") {
		t.Fatalf("install must print the piped create-master command; out=%s", out)
	}
}

// TestInstallShSecuresExistingEnv 钉住已有 .env 的权限（派单 F11）。
// 预置 0644 的 .env：安装后必须是 0600，否则同机其他用户可读密钥。
func TestInstallShSecuresExistingEnv(t *testing.T) {
	requireBash(t)
	root := t.TempDir()
	binDir := t.TempDir()
	sysctlLog := filepath.Join(t.TempDir(), "systemctl.log")
	serverBin := fakeBin(t, binDir, "yiban-server", "while :; do sleep 1; done")
	cliBin := fakeBin(t, binDir, "yiban-cli", "exit 0\n")
	envDir := filepath.Join(root, "etc/yiban")
	if err := os.MkdirAll(envDir, 0o755); err != nil {
		t.Fatalf("mkdir env dir: %v", err)
	}
	envPath := filepath.Join(envDir, ".env")
	if err := os.WriteFile(envPath, []byte("YIBAN_ENV=production\n"), 0o644); err != nil {
		t.Fatalf("write .env: %v", err)
	}
	// 确保预置位确实是 0644：umask 可能压掉组/其他位。
	if err := os.Chmod(envPath, 0o644); err != nil {
		t.Fatalf("chmod .env: %v", err)
	}

	env := map[string]string{
		"YIBAN_INSTALL_ROOT": root,
		"SYSTEMCTL": fakeBin(t, binDir, "systemctl",
			"echo \"systemctl $*\" >> \""+sysctlLog+"\"\nexit 0\n"),
		"YIBAN_DATABASE_URL": "postgres://yiban:pw@127.0.0.1:5432/yiban",
		"PATH":               binDir + string(os.PathListSeparator) + os.Getenv("PATH"),
	}
	if out, rc := runScript(t, scriptPath(t, "install.sh"), env,
		"--server-bin", serverBin, "--cli-bin", cliBin); rc != 0 {
		t.Fatalf("install rc=%d out=%s", rc, out)
	}
	info, err := os.Stat(envPath)
	if err != nil {
		t.Fatalf("stat .env: %v", err)
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Fatalf(".env 权限 = %04o，want 0600（已有文件也必须收紧）", perm)
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
	// 失败路径不得留下暂存件：.new 是下次安装的直接输入，残留会掩盖失败。
	if _, err := os.Stat(installed + ".new"); err == nil {
		t.Fatalf("failed migration must not leave %s.new", installed)
	}
	if _, err := os.Stat(filepath.Join(root, "usr/local/bin/yiban-cli.new")); err == nil {
		t.Fatalf("failed migration must not leave yiban-cli.new")
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

// ---- update.sh 签名发布物接线（干跑，docs/dev/release-signing.md）----
//
// 被测性质：给了 --manifest/--signature/--pubkey 时，update.sh 在任何文件
// 替换之前验签＋对账 sha256；失败必须点名 (step: signing) 且不碰旧件、
// 不停服务；三件只给一件是用法错误。清单与签名由仓库的
// scripts/release/make-manifest.sh 与 sign.sh 现场生成（顺带钉这两个
// 脚本可用）；密钥用 openssl 现场生成，不留任何仓库痕迹。

// requireOpenssl 在缺 openssl 的环境显性跳过（跳过 ≠ 绿：原因必须可见）。
func requireOpenssl(t *testing.T) {
	t.Helper()
	if _, err := exec.LookPath("openssl"); err != nil {
		t.Skip("SKIP: openssl 不在 PATH，签名干跑 e2e 无法构造")
	}
}

// releaseScriptsPath 返回 scripts/release 下脚本的绝对路径。
func releaseScriptsPath(t *testing.T, name string) string {
	t.Helper()
	p, err := filepath.Abs(filepath.Join("..", "..", "..", "scripts", "release", name))
	if err != nil {
		t.Fatalf("resolve release script: %v", err)
	}
	return p
}

// signedManifest 用 openssl 现场生成 Ed25519 密钥对，再调仓库的
// make-manifest.sh 与 sign.sh 产出清单与签名。返回三者路径。
func signedManifest(t *testing.T, dir string, files ...string) (manifest, sig, pub string) {
	t.Helper()
	key := filepath.Join(dir, "release-key.pem")
	pub = filepath.Join(dir, "release-pubkey.pem")
	if out, err := exec.Command("openssl", "genpkey", "-algorithm", "ED25519",
		"-out", key).CombinedOutput(); err != nil {
		t.Fatalf("openssl genpkey: %v\n%s", err, out)
	}
	if out, err := exec.Command("openssl", "pkey", "-in", key,
		"-pubout", "-out", pub).CombinedOutput(); err != nil {
		t.Fatalf("openssl pkey -pubout: %v\n%s", err, out)
	}
	manifest = filepath.Join(dir, "manifest.json")
	args := append([]string{"bash", releaseScriptsPath(t, "make-manifest.sh"), "9.9.9"}, files...)
	out, err := exec.Command(args[0], args[1:]...).Output()
	if err != nil {
		t.Fatalf("make-manifest.sh: %v\n%s", err, out)
	}
	if err := os.WriteFile(manifest, out, 0o644); err != nil {
		t.Fatalf("write manifest: %v", err)
	}
	sig = filepath.Join(dir, "manifest.sig")
	if out, err := exec.Command("bash", releaseScriptsPath(t, "sign.sh"),
		"--key", key, "--in", manifest, "--out", sig).CombinedOutput(); err != nil {
		t.Fatalf("sign.sh: %v\n%s", err, out)
	}
	return manifest, sig, pub
}

// pinnedFingerprint 算公钥文件内容的 SHA-256（小写十六进制），
// 即 update.sh 与 release-check.sh 比对的那枚信任锚指纹。
func pinnedFingerprint(t *testing.T, pub string) string {
	t.Helper()
	body, err := os.ReadFile(pub)
	if err != nil {
		t.Fatalf("read pubkey %s: %v", pub, err)
	}
	sum := sha256.Sum256(body)
	return hex.EncodeToString(sum[:])
}

// assertUntouchedAndNotStopped 钉住「校验失败不碰旧件」：
// 安装位字节不变、没有 .bak 或 .new 残留、服务从未被 stop。
func assertUntouchedAndNotStopped(t *testing.T, installed, oldBody, sysctlLog string) {
	t.Helper()
	if body := readAll(t, installed); body != oldBody {
		t.Fatalf("installed binary must be untouched:\n%s", body)
	}
	if _, err := os.Stat(installed + ".bak"); err == nil {
		t.Fatalf("no .bak may be created when verification fails")
	}
	if _, err := os.Stat(installed + ".new"); err == nil {
		t.Fatalf("staged %s.new must be cleaned up on failure", installed)
	}
	if logBytes, err := os.ReadFile(sysctlLog); err == nil &&
		strings.Contains(string(logBytes), "stop") {
		t.Fatalf("service must not be stopped before verification passes:\n%s", logBytes)
	}
}

// signingFixture 摆好干跑环境：假 systemctl 日志、已装旧 server 与假 cli。
// 返回 env 与 systemctl 日志路径。
func signingFixture(t *testing.T) (map[string]string, string) {
	t.Helper()
	root := t.TempDir()
	sysctlLog := filepath.Join(t.TempDir(), "systemctl.log")
	binDir := t.TempDir()
	installed := filepath.Join(root, "usr/local/bin/yiban-server")
	if err := os.MkdirAll(filepath.Dir(installed), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(installed, []byte("#!/bin/sh\necho OLD-SERVER\n"), 0o755); err != nil {
		t.Fatalf("seed installed: %v", err)
	}
	// 假 cli 只经 PATH 发现（update.sh 的 probe 与迁移都走安装位/PATH）；
	// 变量本身不参与断言。
	fakeBin(t, binDir, "yiban-cli", `
case "$1" in
  migrate) echo "schema version 99"; exit 0 ;;
  healthcheck) exit 0 ;;
esac
exit 0
`)
	env := map[string]string{
		"YIBAN_INSTALL_ROOT": root,
		"SYSTEMCTL": fakeBin(t, binDir, "systemctl",
			"echo \"systemctl $*\" >> \""+sysctlLog+"\"\nexit 0\n"),
		"PATH": binDir + string(os.PathListSeparator) + os.Getenv("PATH"),
	}
	return env, sysctlLog
}

func TestUpdateShVerifiesSignedManifest(t *testing.T) {
	requireBash(t)
	requireOpenssl(t)
	env, sysctlLog := signingFixture(t)
	binDir := t.TempDir()
	newServer := fakeBin(t, binDir, "yiban-server", "echo NEW-SERVER\n")
	newCLI := fakeBin(t, binDir, "yiban-cli-2", "exit 0\n")
	// 清单按基名对账：新 cli 必须叫 yiban-cli，另起名会被点名 no entry。
	if err := os.Rename(newCLI, filepath.Join(binDir, "yiban-cli")); err != nil {
		t.Fatalf("rename cli: %v", err)
	}
	newCLI = filepath.Join(binDir, "yiban-cli")
	manifest, sig, pub := signedManifest(t, t.TempDir(), newServer, newCLI)
	// 信任锚：钉住本次公钥的指纹（P0）。没有锚时 update.sh fail-closed。
	env["YIBAN_RELEASE_PUBKEY_SHA256"] = pinnedFingerprint(t, pub)

	out, rc := runScript(t, scriptPath(t, "update.sh"), env,
		"--server-bin", newServer, "--cli-bin", newCLI,
		"--manifest", manifest, "--signature", sig, "--pubkey", pub)
	if rc != 0 {
		t.Fatalf("signed update must succeed: rc=%d out=%s", rc, out)
	}
	if !strings.Contains(out, "signing verification ok") {
		t.Fatalf("must report verification ok; out=%s", out)
	}
	if strings.Contains(out, "skipping release verification") {
		t.Fatalf("signed path must not skip verification; out=%s", out)
	}
	log := readAll(t, sysctlLog)
	if !strings.Contains(log, "stop") || !strings.Contains(log, "start") {
		t.Fatalf("signed update must stop then start the service:\n%s", log)
	}
}

func TestUpdateShRejectsTamperedManifest(t *testing.T) {
	requireBash(t)
	requireOpenssl(t)
	env, sysctlLog := signingFixture(t)
	root := env["YIBAN_INSTALL_ROOT"]
	installed := filepath.Join(root, "usr/local/bin/yiban-server")
	oldBody := readAll(t, installed)
	binDir := t.TempDir()
	newServer := fakeBin(t, binDir, "yiban-server", "echo NEW-SERVER\n")
	manifest, sig, pub := signedManifest(t, t.TempDir(), newServer)
	env["YIBAN_RELEASE_PUBKEY_SHA256"] = pinnedFingerprint(t, pub)

	// 篡改清单一个字节：版本号改一位。签名覆盖清单字节，验签必须红。
	tampered := filepath.Join(t.TempDir(), "tampered.json")
	body := []byte(strings.Replace(readAll(t, manifest), "9.9.9", "9.9.8", 1))
	if string(body) == readAll(t, manifest) {
		t.Fatalf("tamper no-op: version literal not found in %s", readAll(t, manifest))
	}
	if err := os.WriteFile(tampered, body, 0o644); err != nil {
		t.Fatalf("write tampered: %v", err)
	}

	out, rc := runScript(t, scriptPath(t, "update.sh"), env,
		"--server-bin", newServer,
		"--manifest", tampered, "--signature", sig, "--pubkey", pub)
	if rc == 0 {
		t.Fatalf("tampered manifest must fail the update; out=%s", out)
	}
	if !strings.Contains(out, "signing verification FAILED (step: signing)") {
		t.Fatalf("must name the signing step; out=%s", out)
	}
	// 不碰旧件：安装位仍是旧字节，没有 .bak，服务从未被 stop。
	if body := readAll(t, installed); body != oldBody {
		t.Fatalf("installed binary must be untouched:\n%s", body)
	}
	if _, err := os.Stat(installed + ".bak"); err == nil {
		t.Fatalf("no .bak may be created on verification failure")
	}
	// 校验失败发生在停服务之前：假 systemctl 要么从未被调用（日志不存在），
	// 要么至少没有 stop。
	if logBytes, err := os.ReadFile(sysctlLog); err == nil &&
		strings.Contains(string(logBytes), "stop") {
		t.Fatalf("service must not be stopped before verification passes:\n%s", logBytes)
	}
}

func TestUpdateShRequiresAllThreeSigningFlags(t *testing.T) {
	requireBash(t)
	requireOpenssl(t)
	env, _ := signingFixture(t)
	binDir := t.TempDir()
	newServer := fakeBin(t, binDir, "yiban-server", "echo NEW-SERVER\n")
	manifest, _, _ := signedManifest(t, t.TempDir(), newServer)

	out, rc := runScript(t, scriptPath(t, "update.sh"), env,
		"--server-bin", newServer, "--manifest", manifest)
	if rc != 2 {
		t.Fatalf("partial signing flags are a usage error: rc=%d out=%s", rc, out)
	}
	if !strings.Contains(out, "must be given together") {
		t.Fatalf("error must explain the rule; out=%s", out)
	}
}

// ---- 信任锚（P0）：发布公钥的指纹必须先钉住 -------------------------------
//
// 被测性质：update.sh 不得接受「随便给的公钥」。攻击者自造密钥签恶意二进制
// 时，指纹对不上必须被拒；管理员没写锚时也必须 fail-closed（exit 2），
// 在任何文件替换与停服务之前。

func TestUpdateShRejectsMissingTrustAnchor(t *testing.T) {
	requireBash(t)
	requireOpenssl(t)
	env, sysctlLog := signingFixture(t)
	root := env["YIBAN_INSTALL_ROOT"]
	installed := filepath.Join(root, "usr/local/bin/yiban-server")
	oldBody := readAll(t, installed)
	binDir := t.TempDir()
	newServer := fakeBin(t, binDir, "yiban-server", "echo NEW-SERVER\n")
	manifest, sig, pub := signedManifest(t, t.TempDir(), newServer)
	// 清掉外部环境可能带来的锚：本用例专测「两者都缺」。
	env["YIBAN_RELEASE_PUBKEY_SHA256"] = ""

	out, rc := runScript(t, scriptPath(t, "update.sh"), env,
		"--server-bin", newServer,
		"--manifest", manifest, "--signature", sig, "--pubkey", pub)
	if rc != 2 {
		t.Fatalf("missing trust anchor must fail closed with rc=2: rc=%d out=%s", rc, out)
	}
	if !strings.Contains(out, "trust-anchor") {
		t.Fatalf("error must name the trust anchor step; out=%s", out)
	}
	assertUntouchedAndNotStopped(t, installed, oldBody, sysctlLog)
}

func TestUpdateShRejectsWrongTrustAnchor(t *testing.T) {
	requireBash(t)
	requireOpenssl(t)
	env, sysctlLog := signingFixture(t)
	root := env["YIBAN_INSTALL_ROOT"]
	installed := filepath.Join(root, "usr/local/bin/yiban-server")
	oldBody := readAll(t, installed)
	binDir := t.TempDir()
	newServer := fakeBin(t, binDir, "yiban-server", "echo MALICIOUS-SERVER\n")
	manifest, sig, pub := signedManifest(t, t.TempDir(), newServer)
	// 管理员钉住的是合法发布钥；这里给的是另一把钥的指纹。
	_, _, otherPub := signedManifest(t, t.TempDir(), newServer)
	env["YIBAN_RELEASE_PUBKEY_SHA256"] = pinnedFingerprint(t, otherPub)

	out, rc := runScript(t, scriptPath(t, "update.sh"), env,
		"--server-bin", newServer,
		"--manifest", manifest, "--signature", sig, "--pubkey", pub)
	if rc != 2 {
		t.Fatalf("wrong trust anchor must be rejected with rc=2: rc=%d out=%s", rc, out)
	}
	if !strings.Contains(out, "trust-anchor") {
		t.Fatalf("error must name the trust anchor step; out=%s", out)
	}
	if body := readAll(t, installed); strings.Contains(body, "MALICIOUS-SERVER") {
		t.Fatalf("attacker binary must never reach the install path:\n%s", body)
	}
	assertUntouchedAndNotStopped(t, installed, oldBody, sysctlLog)
}

// TestUpdateShReadsPinnedFingerprintFile 钉住受管配置这条来源：
// 管理员把指纹写进 <安装根>/release-pubkey.sha256，未设环境变量也必须通过。
func TestUpdateShReadsPinnedFingerprintFile(t *testing.T) {
	requireBash(t)
	requireOpenssl(t)
	env, _ := signingFixture(t)
	root := env["YIBAN_INSTALL_ROOT"]
	binDir := t.TempDir()
	newServer := fakeBin(t, binDir, "yiban-server", "echo NEW-SERVER\n")
	newCLI := fakeBin(t, binDir, "yiban-cli", "exit 0\n")
	manifest, sig, pub := signedManifest(t, t.TempDir(), newServer, newCLI)
	pin := filepath.Join(root, "release-pubkey.sha256")
	if err := os.WriteFile(pin, []byte(pinnedFingerprint(t, pub)+"  release-pubkey.pem\n"), 0o644); err != nil {
		t.Fatalf("write pin file: %v", err)
	}

	out, rc := runScript(t, scriptPath(t, "update.sh"), env,
		"--server-bin", newServer, "--cli-bin", newCLI,
		"--manifest", manifest, "--signature", sig, "--pubkey", pub)
	if rc != 0 {
		t.Fatalf("pin file must authorize the update: rc=%d out=%s", rc, out)
	}
	if !strings.Contains(out, "signing verification ok") {
		t.Fatalf("must report verification ok; out=%s", out)
	}
}

// TestUpdateShRejectsBinaryNotCoveredByManifest 钉 sha256 对账：
// 签名有效、但二进制字节在签名后被换掉 ⇒ 必须拒绝且不碰旧件。
func TestUpdateShRejectsBinaryNotCoveredByManifest(t *testing.T) {
	requireBash(t)
	requireOpenssl(t)
	env, sysctlLog := signingFixture(t)
	root := env["YIBAN_INSTALL_ROOT"]
	installed := filepath.Join(root, "usr/local/bin/yiban-server")
	oldBody := readAll(t, installed)
	binDir := t.TempDir()
	newServer := fakeBin(t, binDir, "yiban-server", "echo NEW-SERVER\n")
	manifest, sig, pub := signedManifest(t, t.TempDir(), newServer)
	env["YIBAN_RELEASE_PUBKEY_SHA256"] = pinnedFingerprint(t, pub)
	if err := os.WriteFile(newServer, []byte("#!/bin/sh\necho SWAPPED-SERVER\n"), 0o755); err != nil {
		t.Fatalf("swap binary: %v", err)
	}

	out, rc := runScript(t, scriptPath(t, "update.sh"), env,
		"--server-bin", newServer,
		"--manifest", manifest, "--signature", sig, "--pubkey", pub)
	if rc != 1 {
		t.Fatalf("binary outside the signed manifest must be rejected: rc=%d out=%s", rc, out)
	}
	if !strings.Contains(out, "sha256 mismatch") {
		t.Fatalf("error must name the sha256 mismatch; out=%s", out)
	}
	assertUntouchedAndNotStopped(t, installed, oldBody, sysctlLog)
}

// TestUpdateShRejectsManifestWithoutServerEntry 钉「清单缺条目」：
// 清单里没有 yiban-server 时不得默认放行。
func TestUpdateShRejectsManifestWithoutServerEntry(t *testing.T) {
	requireBash(t)
	requireOpenssl(t)
	env, sysctlLog := signingFixture(t)
	root := env["YIBAN_INSTALL_ROOT"]
	installed := filepath.Join(root, "usr/local/bin/yiban-server")
	oldBody := readAll(t, installed)
	binDir := t.TempDir()
	newServer := fakeBin(t, binDir, "yiban-server", "echo NEW-SERVER\n")
	newCLI := fakeBin(t, binDir, "yiban-cli", "exit 0\n")
	// 清单只覆盖 cli：server 没有条目。
	manifest, sig, pub := signedManifest(t, t.TempDir(), newCLI)
	env["YIBAN_RELEASE_PUBKEY_SHA256"] = pinnedFingerprint(t, pub)

	out, rc := runScript(t, scriptPath(t, "update.sh"), env,
		"--server-bin", newServer, "--cli-bin", newCLI,
		"--manifest", manifest, "--signature", sig, "--pubkey", pub)
	if rc != 1 {
		t.Fatalf("manifest without yiban-server entry must be rejected: rc=%d out=%s", rc, out)
	}
	if !strings.Contains(out, "manifest has no entry for yiban-server") {
		t.Fatalf("error must name the missing entry; out=%s", out)
	}
	assertUntouchedAndNotStopped(t, installed, oldBody, sysctlLog)
}

// TestUpdateShRejectsUnexpectedVersion 钉防降级：--expect-version 与清单
// 版本不一致时必须拒绝（已签名旧包的重放由此被挡）。
func TestUpdateShRejectsUnexpectedVersion(t *testing.T) {
	requireBash(t)
	requireOpenssl(t)
	env, sysctlLog := signingFixture(t)
	root := env["YIBAN_INSTALL_ROOT"]
	installed := filepath.Join(root, "usr/local/bin/yiban-server")
	oldBody := readAll(t, installed)
	binDir := t.TempDir()
	newServer := fakeBin(t, binDir, "yiban-server", "echo NEW-SERVER\n")
	manifest, sig, pub := signedManifest(t, t.TempDir(), newServer)
	env["YIBAN_RELEASE_PUBKEY_SHA256"] = pinnedFingerprint(t, pub)

	out, rc := runScript(t, scriptPath(t, "update.sh"), env,
		"--server-bin", newServer,
		"--manifest", manifest, "--signature", sig, "--pubkey", pub,
		"--expect-version", "9.9.8")
	if rc != 1 {
		t.Fatalf("version mismatch must be rejected: rc=%d out=%s", rc, out)
	}
	if !strings.Contains(out, "expect-version") {
		t.Fatalf("error must name --expect-version; out=%s", out)
	}
	assertUntouchedAndNotStopped(t, installed, oldBody, sysctlLog)
}

// TestUpdateShUsesPrivateSnapshotOfInputs 钉 P2（2026-10-29 复审）：
// 哈希、验签与安装只读暂存副本；校验开始后改写原路径不得影响结果。
//
// 构造：PATH 前置一个 sha256sum 包装器。第一次调用（指纹比对）时改写
// 原始 server 二进制。旧实现会在 local_check 里读原路径 → 哈希不符 → 失败；
// 新实现读快照 → 通过，且装到安装位的是快照字节。
func TestUpdateShUsesPrivateSnapshotOfInputs(t *testing.T) {
	requireBash(t)
	requireOpenssl(t)
	realSum, err := exec.LookPath("sha256sum")
	if err != nil {
		t.Skip("SKIP: sha256sum 不在 PATH，快照用例无法构造")
	}
	env, _ := signingFixture(t)
	binDir := t.TempDir()
	newServer := fakeBin(t, binDir, "yiban-server", "echo NEW-SERVER\n")
	newCLI := fakeBin(t, binDir, "yiban-cli", "exit 0\n")
	manifest, sig, pub := signedManifest(t, t.TempDir(), newServer, newCLI)
	env["YIBAN_RELEASE_PUBKEY_SHA256"] = pinnedFingerprint(t, pub)

	hookDir := t.TempDir()
	swapMarker := filepath.Join(t.TempDir(), "swapped")
	fakeBin(t, hookDir, "sha256sum",
		"if [ ! -f \""+swapMarker+"\" ]; then\n"+
			"  : > \""+swapMarker+"\"\n"+
			"  printf '#!/bin/sh\necho EVIL-SERVER\n' > \""+newServer+"\"\n"+
			"fi\n"+
			"exec \""+realSum+"\" \"$@\"\n")
	env["PATH"] = hookDir + string(os.PathListSeparator) + env["PATH"]

	out, rc := runScript(t, scriptPath(t, "update.sh"), env,
		"--server-bin", newServer, "--cli-bin", newCLI,
		"--manifest", manifest, "--signature", sig, "--pubkey", pub)
	if rc != 0 {
		t.Fatalf("snapshot must shield verification from later swaps: rc=%d out=%s", rc, out)
	}
	if _, err := os.Stat(swapMarker); err != nil {
		t.Fatalf("swap hook did not fire; test did not exercise the window")
	}
	installed := filepath.Join(env["YIBAN_INSTALL_ROOT"], "usr/local/bin/yiban-server")
	body := readAll(t, installed)
	if !strings.Contains(body, "NEW-SERVER") || strings.Contains(body, "EVIL-SERVER") {
		t.Fatalf("installed binary must be the snapshot, not the swapped original:\n%s", body)
	}
}

// TestUpdateShRejectsWritableAnchorFile 钉 P3（2026-10-29 复审）：
// 锚文件组/其他可写 ⇒ exit 2。可写锚等于任何人都能换锚。
// 属主校验只在真机（无 YIBAN_INSTALL_ROOT 前缀）执行；干跑下只验权限位。
func TestUpdateShRejectsWritableAnchorFile(t *testing.T) {
	requireBash(t)
	requireOpenssl(t)
	env, sysctlLog := signingFixture(t)
	root := env["YIBAN_INSTALL_ROOT"]
	installed := filepath.Join(root, "usr/local/bin/yiban-server")
	oldBody := readAll(t, installed)
	binDir := t.TempDir()
	newServer := fakeBin(t, binDir, "yiban-server", "echo NEW-SERVER\n")
	manifest, sig, pub := signedManifest(t, t.TempDir(), newServer)
	pin := filepath.Join(root, "release-pubkey.sha256")
	if err := os.WriteFile(pin, []byte(pinnedFingerprint(t, pub)+"  release-pubkey.pem\n"), 0o644); err != nil {
		t.Fatalf("write pin file: %v", err)
	}
	if err := os.Chmod(pin, 0o666); err != nil {
		t.Fatalf("chmod pin file: %v", err)
	}

	out, rc := runScript(t, scriptPath(t, "update.sh"), env,
		"--server-bin", newServer,
		"--manifest", manifest, "--signature", sig, "--pubkey", pub)
	if rc != 2 {
		t.Fatalf("group/other-writable anchor must fail closed with rc=2: rc=%d out=%s", rc, out)
	}
	if !strings.Contains(out, "trust-anchor") || !strings.Contains(out, "writable") {
		t.Fatalf("error must name the anchor permission problem; out=%s", out)
	}
	assertUntouchedAndNotStopped(t, installed, oldBody, sysctlLog)
}
