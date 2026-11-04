package main

// update.sh 的 U2 一键更新契约（干跑，不碰真 /etc、不需要 root）。
//
// # 功能
// 钉住 U2 新增的四件事：
//   1. 自检门：yiban-cli selfcheck --json 有 FAIL → exit 2，不停服、不写安装位；
//      --selfcheck-only 只读跑同一判据，零写入。
//   2. 统一备份：<状态目录>/backups/<ts>/ 下 db.dump/db.list/db-schema.txt/
//      bin/ 与 SHA256SUMS；目录 0700、文件 0600；--keep-backups 保留最近 N 份。
//   3. 状态与日志：update-state.json（result/exit_code/version/backup_dir/
//      db_schema_before/after/db_restore）与 update.log。
//   4. 数据库回滚：迁移后健康失败时默认恢复到新库并原子切 .env 的
//      YIBAN_DATABASE_URL；原库不动；恢复失败退出 2 + manual action，
//      且不启动旧件（库状态未知）。
//
// # 口径
// 假 psql/pg_dump/pg_restore 只回答 update.sh 真正问的问题；
// 假 CLI 的 migrate 会推进「库版本」文件、healthcheck 恒失败，
// 用来构造「迁移已应用、健康失败」这一条回滚路径。
// 无签名调用点显式带 --allow-unsigned（默认强制验签的逃生口）。
// Windows 无 bash：整文件跳过并打印原因（同 install_update_e2e_test.go）。

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// u2Fixture 是一套 U2 干跑环境。
type u2Fixture struct {
	root     string
	binDir   string
	env      map[string]string
	callLog  string
	sysctl   string
	sysctlLo string
	schema   string // 假库的 goose 版本文件
}

// writeU2PgStubs 装 pg 客户端桩：
//   - psql 回答 CREATEDB（可被 /tmp 标记改成 f）、goose 版本（主库读版本文件，
//     yiban_restore_* 恒为备份基线）、public 基表数、current_database；
//   - pg_dump 记录调用并按 --file= 建出转储；
//   - pg_restore 记录调用；--list 打印 TOC；真实恢复按 RESTORE_FAIL 标记失败。
func (f *u2Fixture) writeU2PgStubs(t *testing.T) {
	t.Helper()
	fakeBin(t, f.binDir, "pg_dump", `
prev=""
for a in "$@"; do
  case "$a" in --file=*) prev="${a#--file=}" ;; esac
done
echo "pg_dump $*" >> "$U2_CALL_LOG"
if [ -n "$prev" ]; then printf 'PGDMP' > "$prev"; fi
exit 0
`)
	fakeBin(t, f.binDir, "pg_restore", `
db=""; prev=""
for a in "$@"; do
  case "$prev" in --dbname) db="$a" ;; esac
  case "$a" in --dbname=*) db="${a#--dbname=}" ;; esac
  case "$a" in --list) echo "; TOC"; echo "1; 1259 16385 TABLE public users yiban"; exit 0 ;; esac
  prev="$a"
done
echo "pg_restore $*" >> "$U2_CALL_LOG"
if [ -f "$U2_RESTORE_FAIL" ]; then
  echo "pg_restore: error: could not execute query: ERROR: disk full" >&2
  exit 1
fi
echo "restored $db" >> "$U2_CALL_LOG"
exit 0
`)
	fakeBin(t, f.binDir, "psql", `
db="$(printf '%s' "$1" | sed -n 's#^[a-z]*://[^/]*/\([^?]*\).*#\1#p')"
echo "psql $*" >> "$U2_CALL_LOG"
case "$*" in
  *rolcreatedb*)
    if [ -f "$U2_NO_CREATEDB" ]; then echo f; else echo t; fi
    ;;
  *goose_db_version*)
    case "$db" in
      *restore*) printf '%s' "$(cat "$U2_BACKUP_SCHEMA")" ;;
      *) cat "$U2_SCHEMA" ;;
    esac
    ;;
  *information_schema.tables*) echo 20 ;;
  *current_database*) printf '%s' "$db" ;;
  *CREATE\ DATABASE*) echo "created $*" >> "$U2_CALL_LOG"; echo 0 ;;
  *) echo 0 ;;
esac
exit 0
`)
}

// newU2Fixture 摆好一趟可跑的 U2 环境：已装 server/cli、.env、假 systemctl。
func newU2Fixture(t *testing.T) *u2Fixture {
	t.Helper()
	f := &u2Fixture{
		root:    t.TempDir(),
		binDir:  t.TempDir(),
		callLog: filepath.Join(t.TempDir(), "calls.log"),
		schema:  filepath.Join(t.TempDir(), "schema"),
	}
	f.sysctlLo = filepath.Join(t.TempDir(), "systemctl.log")
	f.sysctl = fakeBin(t, f.binDir, "systemctl",
		"echo \"systemctl $*\" >> \""+f.sysctlLo+"\"\nexit 0\n")
	if err := os.WriteFile(f.schema, []byte("16"), 0o644); err != nil {
		t.Fatalf("seed schema: %v", err)
	}
	installed := filepath.Join(f.root, "usr/local/bin/yiban-server")
	if err := os.MkdirAll(filepath.Dir(installed), 0o755); err != nil {
		t.Fatalf("mkdir bin: %v", err)
	}
	if err := os.WriteFile(installed, []byte("#!/bin/sh\necho OLD-SERVER\n"), 0o755); err != nil {
		t.Fatalf("seed installed: %v", err)
	}
	installFakeUpdateCLI(t, f.root)
	f.writeU2PgStubs(t)
	if err := os.MkdirAll(filepath.Join(f.root, "etc/yiban"), 0o755); err != nil {
		t.Fatalf("mkdir etc: %v", err)
	}
	envBody := "YIBAN_DATABASE_URL=postgres://yiban:pw@127.0.0.1:5432/yiban?sslmode=disable\n" +
		"YIBAN_UI_DIR=/var/lib/yiban/ui\n"
	if err := os.WriteFile(filepath.Join(f.root, "etc/yiban/.env"), []byte(envBody), 0o600); err != nil {
		t.Fatalf("write env: %v", err)
	}
	f.env = map[string]string{
		"YIBAN_INSTALL_ROOT":          f.root,
		"SYSTEMCTL":                   f.sysctl,
		"U2_CALL_LOG":                 f.callLog,
		"U2_SCHEMA":                   f.schema,
		"U2_BACKUP_SCHEMA":            filepath.Join(t.TempDir(), "backup-schema"),
		"U2_NO_CREATEDB":              filepath.Join(t.TempDir(), "no-createdb"),
		"U2_RESTORE_FAIL":             filepath.Join(t.TempDir(), "restore-fail"),
		"YIBAN_HEALTH_RETRIES":        "1",
		"YIBAN_HEALTH_RETRY_INTERVAL": "0",
		"PATH":                        f.binDir + string(os.PathListSeparator) + os.Getenv("PATH"),
	}
	// 备份基线：与初始 schema 一致。
	if err := os.WriteFile(f.env["U2_BACKUP_SCHEMA"], []byte("16"), 0o644); err != nil {
		t.Fatalf("seed backup schema: %v", err)
	}
	return f
}

// statePath 返回本次环境的状态文件路径。
func (f *u2Fixture) statePath() string {
	return filepath.Join(f.root, "var/lib/yiban/updates/update-state.json")
}

// readState 读状态文件并解码为 map。
func (f *u2Fixture) readState(t *testing.T) map[string]any {
	t.Helper()
	body, err := os.ReadFile(f.statePath())
	if err != nil {
		t.Fatalf("read state: %v", err)
	}
	var doc map[string]any
	if err := json.Unmarshal(body, &doc); err != nil {
		t.Fatalf("state is not JSON: %v\n%s", err, body)
	}
	return doc
}

// backupDirs 列出本次环境下的备份目录。
func (f *u2Fixture) backupDirs(t *testing.T) []string {
	t.Helper()
	dirs, err := filepath.Glob(filepath.Join(f.root, "var/lib/yiban/updates/backups/*"))
	if err != nil {
		t.Fatalf("glob backups: %v", err)
	}
	return dirs
}

// newBCLI 造一个「迁移成功 / 健康失败」的新版 CLI：migrate 把假库推到 17。
func (f *u2Fixture) newBCLI(t *testing.T) string {
	t.Helper()
	return fakeUpdateCLI(t, f.binDir, "yiban-cli-new", `  migrate) printf '17' > "$U2_SCHEMA"; exit 0 ;;
  healthcheck) exit 1 ;;`)
}

// TestUpdateShSelfcheckGateStopsBeforeAnyWrite 钉自检门 fail-closed：
// 报告里有一条 FAIL ⇒ exit 2，且不停服、不建状态目录、不产生备份。
func TestUpdateShSelfcheckGateStopsBeforeAnyWrite(t *testing.T) {
	requireBash(t)
	f := newU2Fixture(t)
	failReport := `{
  "schema_version": 1,
  "checks": [
    {"name": "config.ui_dir", "status": "FAIL", "detail": "目录不存在：/var/lib/yiban/ui", "hint": "装页面"}
  ],
  "summary": {"pass": 0, "fail": 1, "skip": 0, "exit_code": 1}
}`
	fakeBin(t, filepath.Join(f.root, "usr/local/bin"), "yiban-cli", `
case "$1" in
  selfcheck) cat <<'JSON'
`+failReport+`
JSON
    exit 1 ;;
esac
exit 0
`)
	newBin := fakeBin(t, f.binDir, "yiban-server-new", "echo NEW\n")

	out, rc := runScript(t, scriptPath(t, "update.sh"), f.env,
		"--server-bin", newBin, "--allow-unsigned")
	if rc != 2 {
		t.Fatalf("selfcheck FAIL 必须 exit 2：rc=%d out=%s", rc, out)
	}
	if !strings.Contains(out, "config.ui_dir") || !strings.Contains(out, "selfcheck FAILED") {
		t.Fatalf("必须点名失败项与自检门：%s", out)
	}
	if _, err := os.Stat(filepath.Join(f.root, "var/lib/yiban/updates")); err == nil {
		t.Fatalf("自检失败不得创建状态目录")
	}
	if n := len(f.backupDirs(t)); n != 0 {
		t.Fatalf("自检失败不得产生备份，got %d", n)
	}
	if body, err := os.ReadFile(f.sysctlLo); err == nil && strings.Contains(string(body), "stop") {
		t.Fatalf("自检失败不得停服：\n%s", body)
	}
}

// TestUpdateShSelfcheckOnlyIsReadOnly 钉 --selfcheck-only：不取锁、不写文件，
// 退出码跟自检结论走（PASS=0，FAIL=2）。
func TestUpdateShSelfcheckOnlyIsReadOnly(t *testing.T) {
	requireBash(t)
	f := newU2Fixture(t)
	out, rc := runScript(t, scriptPath(t, "update.sh"), f.env, "--selfcheck-only")
	if rc != 0 {
		t.Fatalf("PASS 报告：rc=%d out=%s", rc, out)
	}
	if !strings.Contains(out, "selfcheck: 1 过 / 0 败 / 0 跳") {
		t.Fatalf("必须打印人读摘要：%s", out)
	}
	if _, err := os.Stat(filepath.Join(f.root, "var/lib/yiban/updates")); err == nil {
		t.Fatalf("--selfcheck-only 不得写状态目录")
	}
	if _, err := os.Stat(filepath.Join(f.root, "usr/local/bin/yiban-server.lock")); err == nil {
		t.Fatalf("--selfcheck-only 不得取锁（会写安装位）")
	}
}

// TestUpdateShRequiresCreatedbUnlessRestoreDisabled 钉预检：
// 没有 CREATEDB 且未给 --no-restore-db ⇒ exit 2，不进备份、不停服；
// 显式 --no-restore-db 才放行。
func TestUpdateShRequiresCreatedbUnlessRestoreDisabled(t *testing.T) {
	requireBash(t)
	f := newU2Fixture(t)
	if err := os.WriteFile(f.env["U2_NO_CREATEDB"], []byte("x"), 0o644); err != nil {
		t.Fatalf("arm no-createdb: %v", err)
	}
	newBin := fakeBin(t, f.binDir, "yiban-server-new", "echo NEW\n")

	out, rc := runScript(t, scriptPath(t, "update.sh"), f.env,
		"--server-bin", newBin, "--allow-unsigned")
	if rc != 2 {
		t.Fatalf("无 CREATEDB 必须 exit 2：rc=%d out=%s", rc, out)
	}
	if !strings.Contains(out, "CREATEDB") || !strings.Contains(out, "--no-restore-db") {
		t.Fatalf("失败必须点名 CREATEDB 与逃生口：%s", out)
	}
	if body, err := os.ReadFile(f.sysctlLo); err == nil && strings.Contains(string(body), "stop") {
		t.Fatalf("预检失败不得停服：\n%s", body)
	}
	if n := len(f.backupDirs(t)); n != 0 {
		t.Fatalf("预检失败不得产生备份，got %d", n)
	}

	out, rc = runScript(t, scriptPath(t, "update.sh"), f.env,
		"--server-bin", newBin, "--allow-unsigned", "--no-restore-db")
	if rc != 0 {
		t.Fatalf("显式 --no-restore-db 必须放行：rc=%d out=%s", rc, out)
	}
}

// TestUpdateShBackupLayoutStateAndRetention 钉统一备份的落位/权限/保留与状态文件。
func TestUpdateShBackupLayoutStateAndRetention(t *testing.T) {
	requireBash(t)
	f := newU2Fixture(t)
	newBin := fakeBin(t, f.binDir, "yiban-server-new", "echo NEW\n")
	args := []string{"--server-bin", newBin, "--allow-unsigned", "--keep-backups", "2"}

	for i := 0; i < 3; i++ {
		out, rc := runScript(t, scriptPath(t, "update.sh"), f.env, args...)
		if rc != 0 {
			t.Fatalf("第 %d 轮升级失败：rc=%d out=%s", i+1, rc, out)
		}
	}

	dirs := f.backupDirs(t)
	if len(dirs) != 2 {
		t.Fatalf("--keep-backups 2 应保留 2 份备份，got %d: %v", len(dirs), dirs)
	}
	latest := dirs[len(dirs)-1]
	for _, name := range []string{"db.dump", "db.list", "db-schema.txt", "SHA256SUMS", "bin/yiban-server", "bin/yiban-cli"} {
		if _, err := os.Stat(filepath.Join(latest, name)); err != nil {
			t.Fatalf("备份缺 %s: %v", name, err)
		}
	}
	// 目录 0700、文件 0600：备份含凭据密文与审计行。
	if info, err := os.Stat(latest); err != nil {
		t.Fatalf("stat backup dir: %v", err)
	} else if perm := info.Mode().Perm(); perm != 0o700 {
		t.Fatalf("备份目录权限 = %04o, want 0700", perm)
	}
	for _, name := range []string{"db.dump", "db.list", "db-schema.txt", "SHA256SUMS"} {
		if info, err := os.Stat(filepath.Join(latest, name)); err != nil {
			t.Fatalf("stat %s: %v", name, err)
		} else if perm := info.Mode().Perm(); perm != 0o600 {
			t.Fatalf("%s 权限 = %04o, want 0600", name, perm)
		}
	}
	// 状态文件：字段齐、0600、success/0。
	doc := f.readState(t)
	if doc["result"] != "success" || doc["exit_code"].(float64) != 0 {
		t.Fatalf("状态文件应记 success/0：%v", doc)
	}
	for _, key := range []string{"update_id", "started_at", "finished_at", "phase",
		"version_from", "version_to", "backup_dir", "db_schema_before", "db_schema_after", "db_restore"} {
		if _, ok := doc[key]; !ok {
			t.Fatalf("状态文件缺字段 %s：%v", key, doc)
		}
	}
	if info, err := os.Stat(f.statePath()); err != nil {
		t.Fatalf("stat state: %v", err)
	} else if perm := info.Mode().Perm(); perm != 0o600 {
		t.Fatalf("状态文件权限 = %04o, want 0600", perm)
	}
	if body := readAll(t, filepath.Join(f.root, "var/lib/yiban/updates/update.log")); !strings.Contains(body, "result=success") {
		t.Fatalf("update.log 缺结果行：\n%s", body)
	}
}

// TestUpdateShRestoresDatabaseToNewDatabase 钉数据库回滚成功路径：
// 迁移把库从 16 推到 17、健康失败 ⇒ 恢复 pre-migrate 备份到新库、
// 原子切 .env 的 DSN、起旧件；原库不删不改。
func TestUpdateShRestoresDatabaseToNewDatabase(t *testing.T) {
	requireBash(t)
	f := newU2Fixture(t)
	newBin := fakeBin(t, f.binDir, "yiban-server-new", "echo NEW\n")
	newCLI := f.newBCLI(t)

	out, rc := runScript(t, scriptPath(t, "update.sh"), f.env,
		"--server-bin", newBin, "--cli-bin", newCLI, "--allow-unsigned")
	if rc != 1 {
		t.Fatalf("回滚成功应 exit 1：rc=%d out=%s", rc, out)
	}
	if !strings.Contains(out, "db restore") || !strings.Contains(out, "rolled back") {
		t.Fatalf("必须走数据库恢复与回滚分支：%s", out)
	}
	doc := f.readState(t)
	if doc["db_restore"] != "restored" {
		t.Fatalf("状态文件 db_restore 应为 restored：%v", doc)
	}
	target, _ := doc["db_restore_target"].(string)
	if !strings.HasPrefix(target, "yiban_restore_") {
		t.Fatalf("恢复目标库名不对：%q", target)
	}
	envBody := readAll(t, filepath.Join(f.root, "etc/yiban/.env"))
	if !strings.Contains(envBody, "/"+target+"?") {
		t.Fatalf(".env 的 DSN 未切到新库：\n%s", envBody)
	}
	if got := strings.Count(envBody, "YIBAN_DATABASE_URL="); got != 1 {
		t.Fatalf("DSN 必须只出现一次，got %d:\n%s", got, envBody)
	}
	// 原库保留：只允许 CREATE DATABASE，绝不允许 DROP。
	log := readAll(t, f.callLog)
	if !strings.Contains(log, "created") || !strings.Contains(log, "restored") {
		t.Fatalf("缺少建库/恢复调用：\n%s", log)
	}
	if strings.Contains(log, "DROP DATABASE") {
		t.Fatalf("不得删除原库：\n%s", log)
	}
	// 旧件（server 与 cli）都回到安装位。
	if body := readAll(t, filepath.Join(f.root, "usr/local/bin/yiban-server")); !strings.Contains(body, "OLD-SERVER") {
		t.Fatalf("安装位必须是旧 server：\n%s", body)
	}
	if out, rc := runScript(t, filepath.Join(f.root, "usr/local/bin/yiban-cli"), map[string]string{}, "selfcheck", "--json"); rc != 0 {
		t.Fatalf("回滚后安装位 CLI 必须支持 selfcheck（否则下一次门会红）：rc=%d out=%s", rc, out)
	}
	// 服务被停过又起回来。
	if body := readAll(t, f.sysctlLo); !strings.Contains(body, "stop") || !strings.Contains(body, "start") {
		t.Fatalf("回滚必须停服再起服：\n%s", body)
	}
}

// TestUpdateShRestoreFailureStopsAndAsksForManualAction 钉恢复失败：
// exit 2 + manual action，.env 不动，旧件不启动（库状态未知）。
func TestUpdateShRestoreFailureStopsAndAsksForManualAction(t *testing.T) {
	requireBash(t)
	f := newU2Fixture(t)
	if err := os.WriteFile(f.env["U2_RESTORE_FAIL"], []byte("x"), 0o644); err != nil {
		t.Fatalf("arm restore fail: %v", err)
	}
	newBin := fakeBin(t, f.binDir, "yiban-server-new", "echo NEW\n")
	newCLI := f.newBCLI(t)

	out, rc := runScript(t, scriptPath(t, "update.sh"), f.env,
		"--server-bin", newBin, "--cli-bin", newCLI, "--allow-unsigned")
	if rc != 2 {
		t.Fatalf("恢复失败必须 exit 2：rc=%d out=%s", rc, out)
	}
	if !strings.Contains(out, "manual action required") {
		t.Fatalf("必须打印 manual action：%s", out)
	}
	if !strings.Contains(out, "db restore FAILED") {
		t.Fatalf("必须点名数据库恢复失败：%s", out)
	}
	doc := f.readState(t)
	if doc["db_restore"] != "failed" {
		t.Fatalf("状态文件 db_restore 应为 failed：%v", doc)
	}
	envBody := readAll(t, filepath.Join(f.root, "etc/yiban/.env"))
	if strings.Contains(envBody, "yiban_restore_") {
		t.Fatalf("恢复失败不得改 .env：\n%s", envBody)
	}
	// 最后一条 systemctl 必须是 stop：库状态未知时不自动起旧件。
	log := readAll(t, f.sysctlLo)
	lines := strings.Split(strings.TrimSpace(log), "\n")
	if len(lines) == 0 || !strings.HasPrefix(lines[len(lines)-1], "systemctl stop") {
		t.Fatalf("恢复失败后不得启动旧件：\n%s", log)
	}
}
