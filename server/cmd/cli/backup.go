// backup/restore 子命令：pg_dump 备份、GPG 对称加密、恢复后行数对账。
//
// # 功能
// backup：pg_dump（DSN 带 search_path 时按 schema 限量）→ 在转储尾部
// 追加行数 manifest → gpg 对称加密落盘。任一外部工具缺失即显性报错，
// **不**产出明文归档（看着有备份比没备份更危险）。
// restore：gpg 解密 → psql 导入（目标须为空库/schema）→ 跑迁移 →
// 打印行数对账表，不一致退出 1。
// 本命令**不**备份 photo 的 blob 目录：blob 在文件系统上，
// 输出里会点名「blob 目录需另行备份」。
//
// # 归属
// cmd/cli 子命令。外部命令经 cmdRunner 注入；计数经 countRows 注入；
// 测试不依赖真实 pg_dump/psql/gpg。
//
// # 口径
// 口令来源：--passphrase-file（必须 0600）> 环境变量 YIBAN_BACKUP_PASSPHRASE。
// 口令经 stdin（--passphrase-fd 0）单跳进 gpg，不进 argv。
// 数据库口令经 0600 的 PGPASSFILE 传给 pg_dump/psql；
// 子进程 argv 只带 -h/-p/-U/-d，避免 /proc/<pid>/cmdline 泄漏。
// 退出码：0 成功；对账不一致或工具失败 1；用法错误 2。
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"runtime"
	"strings"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
)

// manifestMarker 是转储尾部的行数清单标记行前缀。
// 恢复时按它找回备份时的行数基线。
const manifestMarker = "-- yiban-backup-manifest"

// manifestTables 是行数对账的表清单：迁移 00001–00016 的业务表全集。
// 新增迁移加表时必须同批加进本表，否则恢复后丢行不会被发现。
// 完整性由两道门钉住：TestManifestTablesCoverEveryBusinessTable（静态）
// 与 TestManifestTablesCoverLiveSchema（真库 information_schema）。
var manifestTables = []string{
	"account_egress", "account_location", "accounts", "admin_schools",
	"audit_logs", "credentials", "egresses", "password_resets",
	"photo_retention", "photos", "school_tasks", "schools", "sessions",
	"sign_tasks", "time_prefs", "users", "window_cache", "window_overrides",
}

// manifestExemptTables 是**不**进对账的表与理由。
// 只放迁移工具的簿记表：它不是业务数据，备份与恢复由工具自身保证。
// 业务表一张都不豁免；空豁免也必须能解释（当前唯一项即 goose 版本表）。
var manifestExemptTables = map[string]string{
	"goose_db_version": "goose 的迁移版本簿记表，不是业务数据；恢复后由 migrate 重建",
}

// manifestExemptWhitelist 是允许豁免的表名白名单。
// 只有迁移工具的簿记表在册；业务表一张都不许豁免。
// 新表加进 manifestExemptTables 而不在此登记会被门直接判红
// （TestManifestExemptionsAreWhitelisted）：写条理由就双绿是缺陷形状。
var manifestExemptWhitelist = map[string]bool{
	"goose_db_version": true,
}

// cmdRunner 是外部命令的执行缝。
type cmdRunner interface {
	// Run 执行 name args，stdin 为口令等输入。
	// env 非空时追加在进程环境之后（连接口令走 PGPASSFILE）。
	Run(ctx context.Context, name string, args []string, stdin []byte, env []string) ([]byte, error)
}

// execRunner 是生产实现：真实 exec，stderr 折进错误。
type execRunner struct{}

// Run 实现 cmdRunner。
func (execRunner) Run(ctx context.Context, name string, args []string, stdin []byte, env []string) ([]byte, error) {
	cmd := execCommand(ctx, name, args...)
	if len(env) > 0 {
		cmd.Env = append(os.Environ(), env...)
	}
	if stdin != nil {
		cmd.Stdin = strings.NewReader(string(stdin))
	}
	var errBuf strings.Builder
	cmd.Stderr = &errBuf
	out, err := cmd.Output()
	if err != nil {
		return out, fmt.Errorf("%s: %w: %s", name, err, strings.TrimSpace(errBuf.String()))
	}
	return out, nil
}

// backupDeps 收敛两个子命令的全部外供依赖。
type backupDeps struct {
	stdout io.Writer
	stderr io.Writer

	lookPath  func(string) (string, error)
	runner    cmdRunner
	countRows func(ctx context.Context, dsn string) (map[string]int64, error)
	migrate   func(ctx context.Context, dsn string) (int64, error)
	// tty 报告 stdin 是否交互终端；confirm 在交互路径下征询确认。
	tty     func() bool
	confirm func() bool
}

// newBackupDeps 装配生产依赖。
func newBackupDeps(stdout, stderr io.Writer) *backupDeps {
	return &backupDeps{
		stdout:    stdout,
		stderr:    stderr,
		lookPath:  execLookPath,
		runner:    execRunner{},
		countRows: countRowsViaPG,
		migrate:   pg.Migrate,
		tty:       stdinIsTTY,
		confirm:   askConfirm,
	}
}

// resolveDSN 取连接串：--dsn > 环境变量 YIBAN_DATABASE_URL。
func resolveDSN(flagValue string) string {
	if v := strings.TrimSpace(flagValue); v != "" {
		return v
	}
	v, _ := config.Env{}.Lookup("YIBAN_DATABASE_URL")
	return strings.TrimSpace(v)
}

// dsnParts 是 libpq 子进程需要的连接字段。
// 口令单独写 PGPASSFILE，不进 argv：/proc/<pid>/cmdline 全局可读。
type dsnParts struct {
	host     string
	port     string
	user     string
	dbname   string
	password string
}

// parseDSN 解析 URI 与 keyword/value 两种 DSN。
// 解析失败即报错：**不**回退到把整串塞进 argv（那等于把口令写回 /proc）。
func parseDSN(dsn string) (dsnParts, error) {
	if u, err := url.Parse(dsn); err == nil && (u.Scheme == "postgres" || u.Scheme == "postgresql") {
		p := dsnParts{
			host:   u.Hostname(),
			port:   u.Port(),
			dbname: strings.TrimPrefix(u.Path, "/"),
		}
		if u.User != nil {
			p.user = u.User.Username()
			p.password, _ = u.User.Password()
		}
		// 主机与端口可写在查询串里（unix socket 部署常见）。
		if p.host == "" {
			p.host = u.Query().Get("host")
		}
		if p.port == "" {
			p.port = u.Query().Get("port")
		}
		return p, nil
	}
	if p, ok := parseKeywordDSN(dsn); ok {
		return p, nil
	}
	return dsnParts{}, errors.New("无法解析 DSN：既不是 postgres:// URI 也不是 key=value 串")
}

// parseKeywordDSN 解析 `host=... user=... password=...` 形。
// 值两端的成对单引号剥掉：libpq 用单引号承载含空白的值。
func parseKeywordDSN(dsn string) (dsnParts, bool) {
	var p dsnParts
	found := false
	for _, field := range splitKeywordFields(dsn) {
		key, value, ok := strings.Cut(field, "=")
		if !ok || strings.TrimSpace(key) == "" {
			return dsnParts{}, false
		}
		value, ok = unquoteKeyword(value)
		if !ok {
			return dsnParts{}, false
		}
		switch strings.ToLower(strings.TrimSpace(key)) {
		case "host":
			p.host = value
		case "port":
			p.port = value
		case "user":
			p.user = value
		case "dbname":
			p.dbname = value
		case "password":
			p.password = value
		default:
			continue
		}
		found = true
	}
	return p, found
}

// splitKeywordFields 按 libpq conninfo 词法切分 `key = value` 串。
// 返回的每项形如 `key=value`，值保留原始引号与反斜杠转义。
// 规则：= 两侧空白可选；单引号内空白与 = 不切；反斜杠转义下一字符；
// 引号内相邻两个单引号表示一个字面单引号。
// 不是键值形的片段原样返回：调用方按「无 =」判解析失败。
func splitKeywordFields(s string) []string {
	var out []string
	i, n := 0, len(s)
	isSpace := func(c byte) bool { return c == ' ' || c == '\t' || c == '\n' || c == '\r' }
	for i < n {
		for i < n && isSpace(s[i]) {
			i++
		}
		if i >= n {
			break
		}
		keyStart := i
		for i < n && s[i] != '=' && !isSpace(s[i]) {
			i++
		}
		key := s[keyStart:i]
		for i < n && isSpace(s[i]) {
			i++
		}
		if i >= n || s[i] != '=' {
			// 非键值片段：原样交出，调用方判失败。
			out = append(out, key)
			continue
		}
		i++ // '='
		for i < n && isSpace(s[i]) {
			i++
		}
		valueStart := i
		if i < n && s[i] == '\'' {
			i++ // 开引号
			for i < n {
				switch {
				case s[i] == '\\' && i+1 < n:
					i += 2
				case s[i] == '\'' && i+1 < n && s[i+1] == '\'':
					i += 2 // '' = 字面单引号
				case s[i] == '\'':
					i++ // 闭引号
					goto valueDone
				default:
					i++
				}
			}
		} else {
			for i < n && !isSpace(s[i]) {
				i++
			}
		}
	valueDone:
		out = append(out, key+"="+s[valueStart:i])
	}
	return out
}

// unquoteKeyword 去掉值两端的成对单引号，并还原 libpq 转义。
// 引号内反斜杠转义下一字符；两个相邻单引号也是一个字面单引号。
// ok=false 表示引号未闭合：坏 DSN 必须失败关闭，不能把残值当口令。
func unquoteKeyword(v string) (string, bool) {
	if len(v) < 2 || v[0] != '\'' {
		return v, true
	}
	var b strings.Builder
	for i := 1; i < len(v); {
		switch {
		case v[i] == '\\' && i+1 < len(v):
			b.WriteByte(v[i+1])
			i += 2
		case v[i] == '\'' && i+1 < len(v) && v[i+1] == '\'':
			b.WriteByte('\'')
			i += 2
		case v[i] == '\'':
			return b.String(), true
		default:
			b.WriteByte(v[i])
			i++
		}
	}
	return "", false
}

// libpqArgs 把连接字段展开成 pg_dump/psql 的分项参数。
// 口令**不**在 argv 里：调用方通过 PGPASSFILE 传。
func libpqArgs(p dsnParts) []string {
	var args []string
	if p.host != "" {
		args = append(args, "-h", p.host)
	}
	if p.port != "" {
		args = append(args, "-p", p.port)
	}
	if p.user != "" {
		args = append(args, "-U", p.user)
	}
	if p.dbname != "" {
		args = append(args, "-d", p.dbname)
	}
	return args
}

// writePassfile 写 0600 临时口令文件（libpq .pgpass 形状），返回路径与清理函数。
// 无口令时返回空路径与空清理函数：不必让 libpq 读空文件。
func writePassfile(p dsnParts) (string, func(), error) {
	noop := func() {}
	if p.password == "" {
		return "", noop, nil
	}
	f, err := os.CreateTemp("", "yiban-pgpass-*")
	if err != nil {
		return "", noop, err
	}
	path := f.Name()
	cleanup := func() { _ = os.Remove(path) }
	if err := f.Chmod(0o600); err != nil {
		f.Close()
		cleanup()
		return "", noop, err
	}
	line := passfileField(p.host) + ":" + passfileField(p.port) + ":" +
		passfileField(p.dbname) + ":" + passfileField(p.user) + ":" +
		passfileField(p.password) + "\n"
	if _, err := f.WriteString(line); err != nil {
		f.Close()
		cleanup()
		return "", noop, err
	}
	if err := f.Close(); err != nil {
		cleanup()
		return "", noop, err
	}
	return path, cleanup, nil
}

// passfileField 转义 .pgpass 字段：冒号与反斜杠前加反斜杠。
// 空字段按 libpq 语义写成通配符 `*`。
func passfileField(s string) string {
	if s == "" {
		return "*"
	}
	return strings.NewReplacer(`\`, `\\`, ":", `\:`).Replace(s)
}

// schemaOf 从 DSN 里取 search_path 值；没有则返回空串（整库备份）。
func schemaOf(dsn string) string {
	i := strings.Index(dsn, "search_path=")
	if i < 0 {
		return ""
	}
	rest := dsn[i+len("search_path="):]
	for j := 0; j < len(rest); j++ {
		if c := rest[j]; c == '&' || c == ' ' {
			return rest[:j]
		}
	}
	return rest
}

// readPassphrase 取口令：口令文件 > 环境变量。错误即显性失败。
// 口令文件必须是普通文件且 0600：组/其他位非零即可被同机其他用户读走。
func readPassphrase(passFile string) ([]byte, error) {
	if passFile != "" {
		info, err := os.Stat(passFile)
		if err != nil {
			return nil, fmt.Errorf("read passphrase file: %w", err)
		}
		if !info.Mode().IsRegular() {
			return nil, fmt.Errorf("passphrase file %s 不是普通文件", passFile)
		}
		if err := checkPassFileMode(info); err != nil {
			return nil, err
		}
		body, err := os.ReadFile(passFile)
		if err != nil {
			return nil, fmt.Errorf("read passphrase file: %w", err)
		}
		return body, nil
	}
	v, ok := config.Env{}.Lookup("YIBAN_BACKUP_PASSPHRASE")
	if !ok || strings.TrimSpace(v) == "" {
		return nil, errors.New("no passphrase: use --passphrase-file (0600) or YIBAN_BACKUP_PASSPHRASE")
	}
	return []byte(v), nil
}

// checkPassFileMode 拒绝组/其他位非零的口令文件。
// Windows 的 FileMode 权限位是合成的（恒 0666），POSIX 判据在那里
// 无判别力：本函数在 Windows 放行，门禁在 Linux/WSL 上执行
// （见 backup_test.go 的显性 SKIP 行）。
func checkPassFileMode(info os.FileInfo) error {
	if runtime.GOOS == "windows" {
		return nil
	}
	if perm := info.Mode().Perm(); perm&0o077 != 0 {
		return fmt.Errorf("passphrase file %s 权限 %04o：口令文件必须 0600（chmod 600 后重试）",
			info.Name(), perm)
	}
	return nil
}

// gpgCommonArgs 是 gpg 的公共参数：批量、无交互、口令走 stdin fd 0。
var gpgCommonArgs = []string{
	"--batch", "--yes", "--pinentry-mode", "loopback", "--passphrase-fd", "0",
}

// runBackup 是 backup 子命令入口。返回进程退出码。
func runBackup(deps *backupDeps, args []string) int {
	fs := newFlagSet("backup", deps.stderr)
	out := fs.String("out", "", "密文归档输出路径（必填）")
	dsnFlag := fs.String("dsn", "", "覆盖 YIBAN_DATABASE_URL")
	passFile := fs.String("passphrase-file", "", "0600 口令文件（缺省读 YIBAN_BACKUP_PASSPHRASE）")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	dsn := resolveDSN(*dsnFlag)
	if dsn == "" || *out == "" {
		fmt.Fprintln(deps.stderr, "backup: --out 与 --dsn（或 YIBAN_DATABASE_URL）均为必填")
		return 2
	}
	pgDump, err := deps.lookPath("pg_dump")
	if err != nil {
		fmt.Fprintln(deps.stderr, "backup: pg_dump 不可用（postgresql-client 未安装？）：", err)
		return 1
	}
	gpg, err := deps.lookPath("gpg")
	if err != nil {
		fmt.Fprintln(deps.stderr, "backup: gpg 不可用（gpg 未安装？）：明文归档不产出", err)
		return 1
	}
	pass, err := readPassphrase(*passFile)
	if err != nil {
		fmt.Fprintln(deps.stderr, "backup:", err)
		return 1
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
	defer cancel()
	counts, err := deps.countRows(ctx, dsn)
	if err != nil {
		fmt.Fprintln(deps.stderr, "backup: 统计行数失败：", err)
		return 1
	}

	// 连接口令只经 0600 的 PGPASSFILE 传给子进程；
	// argv 只放 -h/-p/-U/-d，避免 /proc/<pid>/cmdline 泄漏。
	parts, err := parseDSN(dsn)
	if err != nil {
		fmt.Fprintln(deps.stderr, "backup:", err)
		return 1
	}
	passfile, cleanup, err := writePassfile(parts)
	if err != nil {
		fmt.Fprintln(deps.stderr, "backup: 写临时口令文件失败：", err)
		return 1
	}
	defer cleanup()
	var connEnv []string
	if passfile != "" {
		connEnv = []string{"PGPASSFILE=" + passfile}
	}

	dump, err := os.CreateTemp("", "yiban-dump-*.sql")
	if err != nil {
		fmt.Fprintln(deps.stderr, "backup: 建临时转储失败：", err)
		return 1
	}
	dumpPath := dump.Name()
	defer os.Remove(dumpPath)
	dump.Close()

	dumpArgs := append([]string{"--no-owner", "--no-privileges", "--file=" + dumpPath},
		libpqArgs(parts)...)
	if schema := schemaOf(dsn); schema != "" {
		dumpArgs = append([]string{"-n", schema}, dumpArgs...)
	}
	if _, err := deps.runner.Run(ctx, pgDump, dumpArgs, nil, connEnv); err != nil {
		fmt.Fprintln(deps.stderr, "backup: pg_dump 失败：", err)
		return 1
	}
	if err := appendManifest(dumpPath, counts); err != nil {
		fmt.Fprintln(deps.stderr, "backup: 写行数清单失败：", err)
		return 1
	}
	if _, err := deps.runner.Run(ctx, gpg,
		append(append([]string{}, gpgCommonArgs...),
			"--symmetric", "--cipher-algo", "AES256", "--output="+*out, dumpPath), pass, nil); err != nil {
		fmt.Fprintln(deps.stderr, "backup: gpg 加密失败：", err)
		return 1
	}

	size, err := os.Stat(*out)
	fmt.Fprintf(deps.stdout, "backup: %s 已生成", *out)
	if err == nil {
		fmt.Fprintf(deps.stdout, "（%d 字节）", size.Size())
	}
	fmt.Fprintf(deps.stdout, "\n行数：")
	for _, t := range manifestTables {
		fmt.Fprintf(deps.stdout, " %s=%d", t, counts[t])
	}
	fmt.Fprintf(deps.stdout, "\n注意：photo 的 blob 目录（YIBAN_PHOTO_DIR）不在本归档内，需另行备份。\n")
	return 0
}

// appendManifest 在转储尾部追加行数清单注释行。
func appendManifest(dumpPath string, counts map[string]int64) error {
	body, err := json.Marshal(counts)
	if err != nil {
		return err
	}
	f, err := os.OpenFile(dumpPath, os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	defer f.Close()
	_, err = fmt.Fprintf(f, "\n%s %s\n", manifestMarker, body)
	return err
}

// parseManifest 从解密后的转储文本里取行数清单。缺失即错误。
func parseManifest(dumpPath string) (map[string]int64, error) {
	body, err := os.ReadFile(dumpPath)
	if err != nil {
		return nil, err
	}
	for _, line := range strings.Split(string(body), "\n") {
		trimmed := strings.TrimSpace(line)
		if after, ok := strings.CutPrefix(trimmed, manifestMarker+" "); ok {
			var counts map[string]int64
			if err := json.Unmarshal([]byte(strings.TrimSpace(after)), &counts); err != nil {
				return nil, fmt.Errorf("manifest 行解析失败: %w", err)
			}
			return counts, nil
		}
	}
	return nil, errors.New("转储里没有行数清单（不是本工具产出的备份？）")
}

// runRestore 是 restore 子命令入口。返回进程退出码。
func runRestore(deps *backupDeps, args []string) int {
	fs := newFlagSet("restore", deps.stderr)
	in := fs.String("in", "", "密文归档路径（必填）")
	dsnFlag := fs.String("dsn", "", "覆盖 YIBAN_DATABASE_URL（目标须为空库/schema）")
	passFile := fs.String("passphrase-file", "", "0600 口令文件（缺省读 YIBAN_BACKUP_PASSPHRASE）")
	force := fs.Bool("force", false, "跳过交互确认（非交互环境必加）")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	dsn := resolveDSN(*dsnFlag)
	if *in == "" || dsn == "" {
		fmt.Fprintln(deps.stderr, "restore: --in 与 --dsn 均为必填")
		return 2
	}
	if !*force {
		if !deps.tty() {
			fmt.Fprintln(deps.stderr, "restore: 拒绝在非交互环境执行；确认覆盖请加 --force")
			return 1
		}
		if !deps.confirm() {
			fmt.Fprintln(deps.stderr, "restore: 未确认，退出")
			return 1
		}
	}
	gpg, err := deps.lookPath("gpg")
	if err != nil {
		fmt.Fprintln(deps.stderr, "restore: gpg 不可用：", err)
		return 1
	}
	psql, err := deps.lookPath("psql")
	if err != nil {
		fmt.Fprintln(deps.stderr, "restore: psql 不可用（postgresql-client 未安装？）：", err)
		return 1
	}
	pass, err := readPassphrase(*passFile)
	if err != nil {
		fmt.Fprintln(deps.stderr, "restore:", err)
		return 1
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
	defer cancel()

	parts, err := parseDSN(dsn)
	if err != nil {
		fmt.Fprintln(deps.stderr, "restore:", err)
		return 1
	}
	passfile, cleanup, err := writePassfile(parts)
	if err != nil {
		fmt.Fprintln(deps.stderr, "restore: 写临时口令文件失败：", err)
		return 1
	}
	defer cleanup()
	var connEnv []string
	if passfile != "" {
		connEnv = []string{"PGPASSFILE=" + passfile}
	}

	dump, err := os.CreateTemp("", "yiban-restore-*.sql")
	if err != nil {
		fmt.Fprintln(deps.stderr, "restore: 建临时转储失败：", err)
		return 1
	}
	dumpPath := dump.Name()
	dump.Close()
	defer os.Remove(dumpPath)

	if _, err := deps.runner.Run(ctx, gpg,
		append(append([]string{}, gpgCommonArgs...),
			"--decrypt", "--output="+dumpPath, *in), pass, nil); err != nil {
		fmt.Fprintln(deps.stderr, "restore: gpg 解密失败（口令不对或归档损坏）：", err)
		return 1
	}
	baseline, err := parseManifest(dumpPath)
	if err != nil {
		fmt.Fprintln(deps.stderr, "restore:", err)
		return 1
	}

	psqlArgs := append([]string{"--set", "ON_ERROR_STOP=1", "--file=" + dumpPath},
		libpqArgs(parts)...)
	if _, err := deps.runner.Run(ctx, psql, psqlArgs, nil, connEnv); err != nil {
		fmt.Fprintln(deps.stderr, "restore: psql 导入失败（目标须为空；schema 限量的转储会自建 schema，勿预建）：", err)
		return 1
	}
	version, err := deps.migrate(ctx, dsn)
	if err != nil {
		fmt.Fprintln(deps.stderr, "restore: 恢复后迁移失败：", err)
		return 1
	}
	fmt.Fprintf(deps.stdout, "restore: 导入完成，schema 版本 %d\n", version)

	restored, err := deps.countRows(ctx, dsn)
	if err != nil {
		fmt.Fprintln(deps.stderr, "restore: 统计恢复后行数失败：", err)
		return 1
	}

	match := true
	fmt.Fprintln(deps.stdout, "行数对账（备份时 / 恢复后）：")
	for _, t := range manifestTables {
		state := "一致"
		if baseline[t] != restored[t] {
			state = "不一致"
			match = false
		}
		fmt.Fprintf(deps.stdout, "  %-12s %d / %d  %s\n", t, baseline[t], restored[t], state)
	}
	if !match {
		fmt.Fprintln(deps.stdout, "对账不一致：恢复件与备份基线有出入，按失败处理。")
		return 1
	}
	fmt.Fprintln(deps.stdout, "对账一致。")
	fmt.Fprintln(deps.stdout, "提醒：blob 目录（YIBAN_PHOTO_DIR）不在本归档内；恢复到新主机时须另行迁移，否则图片行在、文件缺失。")
	return 0
}

// countRowsViaPG 数 manifestTables 的行数（生产实现）。
func countRowsViaPG(ctx context.Context, dsn string) (map[string]int64, error) {
	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 1, StatementTimeout: 30 * time.Second})
	if err != nil {
		return nil, err
	}
	defer db.Close()
	counts := map[string]int64{}
	for _, t := range manifestTables {
		n, err := db.QueryInt64(ctx, "SELECT count(*) FROM "+t)
		if err != nil {
			return nil, fmt.Errorf("count %s: %w", t, err)
		}
		counts[t] = n
	}
	return counts, nil
}
