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
	"strconv"
	"strings"
	"testing"
)

// u2CanaryPassword 是干跑环境里的 DSN 口令。它只允许出现在 .env 与
// pgpass 文件里；任何 pg 客户端的 argv 出现它就是缺陷（HIGH）。
const u2CanaryPassword = "c4n4ry-pw-with-:colon"

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
if [ -n "${U2_ARGV_LOG:-}" ]; then printf 'pg_dump %s\n' "$*" >> "$U2_ARGV_LOG"; fi
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
if [ -n "${U2_ARGV_LOG:-}" ]; then printf 'pg_restore %s\n' "$*" >> "$U2_ARGV_LOG"; fi
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
if [ -n "${U2_ARGV_LOG:-}" ]; then printf 'psql %s pgpass=%s\n' "$*" "${PGPASSFILE:-<unset>}" >> "$U2_ARGV_LOG"; fi
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
	// canary 口令：测试断言它不出现在任何 pg 客户端的 argv 里。
	envBody := "YIBAN_DATABASE_URL=postgres://yiban:" + u2CanaryPassword +
		"@127.0.0.1:5432/yiban?sslmode=disable\n" +
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
		"U2_ARGV_LOG":                 filepath.Join(t.TempDir(), "argv.log"),
		"FAKE_SELFCHECK_ARGS":         filepath.Join(t.TempDir(), "selfcheck-args.log"),
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
	dirs, err := filepath.Glob(filepath.Join(f.root, "var/lib/yiban/backups/*"))
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
	for _, name := range []string{"db.dump", "db.list", "db-schema.txt", "env.backup",
		"SHA256SUMS", "bin/yiban-server", "bin/yiban-cli"} {
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
	for _, name := range []string{"db.dump", "db.list", "db-schema.txt", "env.backup", "SHA256SUMS"} {
		if info, err := os.Stat(filepath.Join(latest, name)); err != nil {
			t.Fatalf("stat %s: %v", name, err)
		} else if perm := info.Mode().Perm(); perm != 0o600 {
			t.Fatalf("%s 权限 = %04o, want 0600", name, perm)
		}
	}
	// .env 快照里必须有 DSN 原文（回滚会改写它，人工回退要用）。
	if body := readAll(t, filepath.Join(latest, "env.backup")); !strings.Contains(body, "YIBAN_DATABASE_URL=") {
		t.Fatalf("env.backup 不含 .env 原文：\n%s", body)
	}
	// db-schema.txt 记库名，状态里也有 db_name_before。
	if body := readAll(t, filepath.Join(latest, "db-schema.txt")); !strings.Contains(body, "database=yiban") {
		t.Fatalf("db-schema.txt 缺 database=：\n%s", body)
	}
	// 状态文件：字段齐、0600、success/0。
	doc := f.readState(t)
	if doc["db_name_before"] != "yiban" {
		t.Fatalf("状态 db_name_before 应为 yiban：%v", doc)
	}
	if doc["result"] != "success" || doc["exit_code"].(float64) != 0 {
		t.Fatalf("状态文件应记 success/0：%v", doc)
	}
	for _, key := range []string{"update_id", "started_at", "finished_at", "phase",
		"version_from", "version_to", "backup_dir", "db_name_before", "db_name_after",
		"db_schema_before", "db_schema_after", "db_restore"} {
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
	// M6：CREATE DATABASE 已成功但恢复失败 → 目标名必须记在状态里，
	// manual action 也必须给出真名（占位名会让人工撞名或找不到库）。
	target, _ := doc["db_restore_target"].(string)
	if !strings.HasPrefix(target, "yiban_restore_") {
		t.Fatalf("恢复失败时 db_restore_target 必须是真库名，got %q", target)
	}
	if !strings.Contains(out, target) {
		t.Fatalf("manual action 必须点出真实目标库 %q：%s", target, out)
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

// ---- 审查补测（HIGH/MEDIUM 逐项） ------------------------------------------

// useFailingStart 把假 systemctl 换成「前 n 次 start 失败」的版本。
// start 失败是 HIGH 1 的靶子：update.sh 必须走回滚，不能让新二进制留在
// 安装位、服务停着却退 1。
func (f *u2Fixture) useFailingStart(t *testing.T, n int) {
	t.Helper()
	counter := filepath.Join(t.TempDir(), "start-fail-left")
	if err := os.WriteFile(counter, []byte(strconv.Itoa(n)), 0o644); err != nil {
		t.Fatalf("seed start counter: %v", err)
	}
	f.sysctl = fakeBin(t, f.binDir, "systemctl", `
echo "systemctl $*" >> "`+f.sysctlLo+`"
case "$1" in
  start)
    left="$(cat "`+counter+`" 2>/dev/null || echo 0)"
    if [ "${left:-0}" -gt 0 ]; then
      echo $((left - 1)) > "`+counter+`"
      echo "systemctl: start failed (injected)" >&2
      exit 1
    fi
    ;;
esac
exit 0
`)
	f.env["SYSTEMCTL"] = f.sysctl
}

// TestUpdateShSystemctlStartFailureRollsBack 钉 HIGH 1：
// start 失败绝不能裸退（set -e）把新二进制留在安装位；必须回滚旧件。
//
//	分支一：只有第一次 start 失败 → 回滚成功 → exit 1、安装位是旧件。
//	分支二：start 一直失败 + 探活也失败 → exit 2、打印 manual action。
func TestUpdateShSystemctlStartFailureRollsBack(t *testing.T) {
	requireBash(t)
	f := newU2Fixture(t)
	f.useFailingStart(t, 1)
	newBin := fakeBin(t, f.binDir, "yiban-server-new", "echo NEW\n")
	oldBody := readAll(t, filepath.Join(f.root, "usr/local/bin/yiban-server"))

	out, rc := runScript(t, scriptPath(t, "update.sh"), f.env,
		"--server-bin", newBin, "--allow-unsigned")
	if rc != 1 {
		t.Fatalf("start 失败但回滚成功应 exit 1：rc=%d out=%s", rc, out)
	}
	if !strings.Contains(out, "start FAILED after the swap") {
		t.Fatalf("必须点名 start 失败：%s", out)
	}
	if !strings.Contains(out, "rolled back") {
		t.Fatalf("必须走回滚：%s", out)
	}
	if got := readAll(t, filepath.Join(f.root, "usr/local/bin/yiban-server")); got != oldBody {
		t.Fatalf("安装位必须是旧件（新件不能留下）：\n%s", got)
	}
	if _, err := os.Stat(filepath.Join(f.root, "usr/local/bin/yiban-server.bak")); err == nil {
		t.Fatalf(".bak 应已换回安装位（不应残留）")
	}
	doc := f.readState(t)
	if doc["result"] != "failed" || doc["exit_code"].(float64) != 1 {
		t.Fatalf("状态应为 failed/1：%v", doc)
	}

	// 分支二：start 恒失败 + 探活恒失败 → 回滚不回来，exit 2。
	f2 := newU2Fixture(t)
	f2.useFailingStart(t, 99)
	healthFail := filepath.Join(t.TempDir(), "health-fail")
	if err := os.WriteFile(healthFail, []byte("x"), 0o644); err != nil {
		t.Fatalf("arm health fail: %v", err)
	}
	fakeUpdateCLI(t, filepath.Join(f2.root, "usr/local/bin"), "yiban-cli", `  migrate) exit 0 ;;
  healthcheck) [ -f "$FAKE_HEALTH_FAIL" ] && exit 1; exit 0 ;;`)
	f2.env["FAKE_HEALTH_FAIL"] = healthFail
	newBin2 := fakeBin(t, f2.binDir, "yiban-server-new", "echo NEW\n")
	out, rc = runScript(t, scriptPath(t, "update.sh"), f2.env,
		"--server-bin", newBin2, "--allow-unsigned")
	if rc != 2 {
		t.Fatalf("回滚后仍不健康应 exit 2：rc=%d out=%s", rc, out)
	}
	if !strings.Contains(out, "manual action required") {
		t.Fatalf("必须打印 manual action：%s", out)
	}
}

// TestUpdateShFirstUIInstallRollsBackOnHealthFailure 钉 HIGH 2：
// 首次装页面（没有旧 ui 目录）时健康失败，必须删掉新页面并撤掉本轮写的
// YIBAN_UI_DIR——旧二进制配新页面是明确禁止的混装。
func TestUpdateShFirstUIInstallRollsBackOnHealthFailure(t *testing.T) {
	requireBash(t)
	f := newU2Fixture(t)
	// 只装 API：.env 里没有 YIBAN_UI_DIR，也没有 ui 目录。
	if err := os.WriteFile(filepath.Join(f.root, "etc/yiban/.env"),
		[]byte("YIBAN_DATABASE_URL=postgres://yiban:"+u2CanaryPassword+"@127.0.0.1:5432/yiban?sslmode=disable\n"),
		0o600); err != nil {
		t.Fatalf("write env: %v", err)
	}
	newBin := fakeBin(t, f.binDir, "yiban-server-new", "echo NEW\n")
	// 新版：迁移成功、探活恒失败 → 换件后必失败。
	newCLI := fakeUpdateCLI(t, f.binDir, "yiban-cli-new", `  migrate) exit 0 ;;
  healthcheck) exit 1 ;;`)
	ui := writeUIDistDir(t, "first-ui")

	out, rc := runScript(t, scriptPath(t, "update.sh"), f.env,
		"--server-bin", newBin, "--cli-bin", newCLI, "--ui-dir", ui, "--allow-unsigned")
	if rc != 1 {
		t.Fatalf("回滚成功应 exit 1：rc=%d out=%s", rc, out)
	}
	uiDir := filepath.Join(f.root, "var/lib/yiban/ui")
	if _, err := os.Stat(uiDir); err == nil {
		t.Fatalf("首次安装回滚后不得留下新页面目录：%s", uiDir)
	}
	if body := readAll(t, filepath.Join(f.root, "etc/yiban/.env")); strings.Contains(body, "YIBAN_UI_DIR=") {
		t.Fatalf("首次安装回滚后必须撤掉 YIBAN_UI_DIR：\n%s", body)
	}
	if _, err := os.Stat(uiDir + ".new"); err == nil {
		t.Fatalf("不得残留 ui.new")
	}
	if out, _ := runScript(t, filepath.Join(f.root, "usr/local/bin/yiban-cli"),
		map[string]string{}, "selfcheck", "--json"); !strings.Contains(out, "schema_version") {
		t.Fatalf("回滚后安装位 CLI 必须是旧件（能跑 selfcheck）：%s", out)
	}
}

// TestUpdateShSelfcheckGatePassesBackupDir 钉 HIGH 3 的接线：
// 自检门必须显式传 --backup-dir（判权对象＝真正写入的备份根），
// 不能吃 U1 的历史缺省（老机上那是 root 0700 的 pre-migrate）。
func TestUpdateShSelfcheckGatePassesBackupDir(t *testing.T) {
	requireBash(t)
	f := newU2Fixture(t)
	newBin := fakeBin(t, f.binDir, "yiban-server-new", "echo NEW\n")
	out, rc := runScript(t, scriptPath(t, "update.sh"), f.env,
		"--server-bin", newBin, "--allow-unsigned")
	if rc != 0 {
		t.Fatalf("update rc=%d out=%s", rc, out)
	}
	args := readAll(t, f.env["FAKE_SELFCHECK_ARGS"])
	wantBackup := filepath.Join(f.root, "var/lib/yiban/backups")
	if !strings.Contains(args, "--backup-dir "+wantBackup) {
		t.Fatalf("自检门没有传 --backup-dir %s：\n%s", wantBackup, args)
	}
	if strings.Contains(args, "pre-migrate") {
		t.Fatalf("自检门仍在用历史 pre-migrate 目录：\n%s", args)
	}
}

// TestUpdateShKeepsPasswordOutOfArgv 钉 HIGH 4：
// 所有 psql/pg_dump/pg_restore 的 argv 都不含 DSN 口令；口令只经 0600
// 的 PGPASSFILE 传给 libpq，进程退出后该文件被删。
func TestUpdateShKeepsPasswordOutOfArgv(t *testing.T) {
	requireBash(t)
	f := newU2Fixture(t)
	newBin := fakeBin(t, f.binDir, "yiban-server-new", "echo NEW\n")
	// 走一次带数据库回滚的失败路径：迁移推进一档、健康失败 →
	// psql/pg_dump/pg_restore 都会被调用（argv 才有可判材料）。
	newCLI := f.newBCLI(t)
	out, rc := runScript(t, scriptPath(t, "update.sh"), f.env,
		"--server-bin", newBin, "--cli-bin", newCLI, "--allow-unsigned")
	if rc != 1 {
		t.Fatalf("回滚成功应 exit 1：rc=%d out=%s", rc, out)
	}
	argvLog := readAll(t, f.env["U2_ARGV_LOG"])
	if strings.Contains(argvLog, u2CanaryPassword) {
		t.Fatalf("pg 客户端 argv 泄漏了口令：\n%s", argvLog)
	}
	if !strings.Contains(argvLog, "postgres://yiban@") {
		t.Fatalf("argv 里应保留去掉口令的 DSN（证明走的是同一连接串）：\n%s", argvLog)
	}
	if !strings.Contains(argvLog, "pgpass=") || strings.Contains(argvLog, "pgpass=<unset>") {
		t.Fatalf("psql 必须拿到 PGPASSFILE：\n%s", argvLog)
	}
	// pgpass 文件在退出后必须删掉（含带口令的副本）。
	for _, line := range strings.Split(argvLog, "\n") {
		idx := strings.Index(line, "pgpass=")
		if idx < 0 {
			continue
		}
		path := line[idx+len("pgpass="):]
		if strings.Contains(path, " ") {
			path = path[:strings.Index(path, " ")]
		}
		if path == "" || path == "<unset>" {
			continue
		}
		// 文件在暂存目录里，随暂存目录一起删。
		if _, err := os.Stat(path); err == nil {
			t.Fatalf("pgpass 文件退出后仍在：%s", path)
		}
	}
}

// TestUpdateShMissingFlagValueIsUsageError 钉 MEDIUM 5：
// 缺值/空值必须是用法错误（exit 2），不是 ${2:?} 的 1。
func TestUpdateShMissingFlagValueIsUsageError(t *testing.T) {
	requireBash(t)
	f := newU2Fixture(t)
	for _, args := range [][]string{
		{"--server-bin"},
		{"--server-bin", "", "--allow-unsigned"},
		{"--server-bin", "/bin/true", "--allow-unsigned", "--keep-backups"},
		{"--server-bin", "/bin/true", "--allow-unsigned", "--backup-dir", ""},
	} {
		out, rc := runScript(t, scriptPath(t, "update.sh"), f.env, args...)
		if rc != 2 {
			t.Fatalf("%v 缺值应 exit 2：rc=%d out=%s", args, rc, out)
		}
	}
}
