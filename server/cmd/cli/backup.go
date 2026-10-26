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
// 口令来源：--passphrase-file（0600）> 环境变量 YIBAN_BACKUP_PASSPHRASE。
// 口令经 stdin（--passphrase-fd 0）单跳进 gpg，不进 argv 与子进程 env。
// 退出码：0 成功；对账不一致或工具失败 1；用法错误 2。
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
)

// manifestMarker 是转储尾部的行数清单标记行前缀。
// 恢复时按它找回备份时的行数基线。
const manifestMarker = "-- yiban-backup-manifest"

// manifestTables 是行数对账的表清单。
// photos 只对 photos 表计数；photo 的 blob 字节在文件系统上，
// 不在本归档内，输出会另行点名。
var manifestTables = []string{"users", "accounts", "school_tasks", "sign_tasks", "photos"}

// cmdRunner 是外部命令的执行缝。
type cmdRunner interface {
	// Run 执行 name args，stdin 为口令等输入；失败时错误含 stderr。
	Run(ctx context.Context, name string, args []string, stdin []byte) ([]byte, error)
}

// execRunner 是生产实现：真实 exec，stderr 折进错误。
type execRunner struct{}

// Run 实现 cmdRunner。
func (execRunner) Run(ctx context.Context, name string, args []string, stdin []byte) ([]byte, error) {
	cmd := execCommand(ctx, name, args...)
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

// libpqDSN 去掉 DSN 里的 search_path 参数：libpq（pg_dump/psql）的
// URI 解析不认它，而转储与导入都用 schema 限定名，不依赖 search_path。
// Go 侧（pgx）仍用原 DSN：两族工具的参数面不同。
func libpqDSN(dsn string) string {
	i := strings.Index(dsn, "search_path=")
	if i < 0 {
		return dsn
	}
	start, end := i, i+len("search_path=")
	for end < len(dsn) && dsn[end] != '&' && dsn[end] != ' ' {
		end++
	}
	for start > 0 && (dsn[start-1] == '&' || dsn[start-1] == ' ') {
		start--
	}
	trimmed := dsn[:start] + dsn[end:]
	trimmed = strings.ReplaceAll(trimmed, "?&", "?")
	trimmed = strings.ReplaceAll(trimmed, "&&", "&")
	return strings.TrimRight(trimmed, "?& ")
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
func readPassphrase(passFile string) ([]byte, error) {
	if passFile != "" {
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

	dump, err := os.CreateTemp("", "yiban-dump-*.sql")
	if err != nil {
		fmt.Fprintln(deps.stderr, "backup: 建临时转储失败：", err)
		return 1
	}
	dumpPath := dump.Name()
	defer os.Remove(dumpPath)
	dump.Close()

	dumpArgs := []string{"--no-owner", "--no-privileges", "--file=" + dumpPath,
		"--dbname=" + libpqDSN(dsn)}
	if schema := schemaOf(dsn); schema != "" {
		dumpArgs = append([]string{"-n", schema}, dumpArgs...)
	}
	if _, err := deps.runner.Run(ctx, pgDump, dumpArgs, nil); err != nil {
		fmt.Fprintln(deps.stderr, "backup: pg_dump 失败：", err)
		return 1
	}
	if err := appendManifest(dumpPath, counts); err != nil {
		fmt.Fprintln(deps.stderr, "backup: 写行数清单失败：", err)
		return 1
	}
	if _, err := deps.runner.Run(ctx, gpg,
		append(append([]string{}, gpgCommonArgs...),
			"--symmetric", "--cipher-algo", "AES256", "--output="+*out, dumpPath), pass); err != nil {
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
			"--decrypt", "--output="+dumpPath, *in), pass); err != nil {
		fmt.Fprintln(deps.stderr, "restore: gpg 解密失败（口令不对或归档损坏）：", err)
		return 1
	}
	baseline, err := parseManifest(dumpPath)
	if err != nil {
		fmt.Fprintln(deps.stderr, "restore:", err)
		return 1
	}

	if _, err := deps.runner.Run(ctx, psql,
		[]string{"--set", "ON_ERROR_STOP=1", "--dbname=" + libpqDSN(dsn), "--file=" + dumpPath}, nil); err != nil {
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
