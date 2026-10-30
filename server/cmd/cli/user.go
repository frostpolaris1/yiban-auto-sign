package main

// user 子命令：`user create-master` 是干净部署建第一个 master 的
// 唯一自助入口（P7 审查 P2-1：没有它，部署完成也无人能登录）。
// 之后的所有建号都走已认证的 POST /api/users。

import (
	"bufio"
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"runtime"
	"strings"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app/user"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/assembly"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coremask "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// runUser 处理 user 子命令。当前只有 create-master。
// stdin 只服务 --password-stdin：口令走管道，不进进程 argv。
func runUser(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	if len(args) == 0 || args[0] != "create-master" {
		fmt.Fprintln(stderr, `用法: yiban-cli user create-master --email <地址> (--password-file <路径> | --password-stdin | --password <口令>) [--dsn <串>] [--pepper <串>]
子命令只有 create-master；后续建号走已认证的 POST /api/users。
生产用 --password-stdin 或 --password-file：--password 会把口令写进进程 argv。`)
		return 2
	}
	fs := flag.NewFlagSet("user create-master", flag.ContinueOnError)
	fs.SetOutput(stderr)
	email := fs.String("email", "", "master 登录地址")
	password := fs.String("password", "", "登录口令（仅测试用：会进进程 argv）")
	passwordFile := fs.String("password-file", "", "从 0600 普通文件读口令（生产推荐）")
	passwordStdin := fs.Bool("password-stdin", false, "从标准输入读一行口令（生产推荐）")
	dsn := fs.String("dsn", "", "覆盖 YIBAN_DATABASE_URL")
	pepper := fs.String("pepper", "", "覆盖 YIBAN_HASH_PEPPER（至少 16 字符）")
	if err := fs.Parse(args[1:]); err != nil {
		return 2
	}
	if strings.TrimSpace(*email) == "" {
		fmt.Fprintln(stderr, "user create-master: --email 必填")
		return 2
	}
	secret, err := resolvePassword(*password, *passwordFile, *passwordStdin, stdin)
	if err != nil {
		fmt.Fprintf(stderr, "user create-master: %v\n", err)
		return 2
	}
	url := strings.TrimSpace(*dsn)
	if url == "" {
		v, _ := config.Env{}.Lookup("YIBAN_DATABASE_URL")
		url = strings.TrimSpace(v)
	}
	if url == "" {
		fmt.Fprintln(stderr, "user create-master: YIBAN_DATABASE_URL 未设置（或用 --dsn 传入）")
		return 2
	}
	pep := strings.TrimSpace(*pepper)
	if pep == "" {
		v, _ := config.Env{}.Lookup("YIBAN_HASH_PEPPER")
		pep = strings.TrimSpace(v)
	}
	if len(pep) < 16 {
		fmt.Fprintln(stderr, "user create-master: YIBAN_HASH_PEPPER 未设置或不足 16 字符（审计链钥由它派生）")
		return 2
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()
	db, err := pg.Open(ctx, pg.Options{DSN: url})
	if err != nil {
		fmt.Fprintf(stderr, "user create-master: %v\n", err)
		return 1
	}
	defer db.Close()
	auditKey, err := assembly.DeriveAuditKey([]byte(pep))
	if err != nil {
		fmt.Fprintf(stderr, "user create-master: %v\n", err)
		return 2
	}
	audits, err := pg.NewAuditRepo(db, auditKey)
	if err != nil {
		fmt.Fprintf(stderr, "user create-master: %v\n", err)
		return 1
	}
	svc, err := user.NewService(user.Deps{
		Users: pg.NewUserRepo(db),
		Audit: audits,
		Tx:    db,
		Clock: coreclock.System{},
		// 一次性 CLI：没有并发面，不引入进程级闸。
		KDF:   coreauth.PlainHasher{},
		Limit: assembly.UserCapacityLimit,
	})
	if err != nil {
		fmt.Fprintf(stderr, "user create-master: %v\n", err)
		return 1
	}
	// 首个 master 没有会话：审计主体记 cli（无邮箱形状，满足 actor_label CHECK）。
	rec, err := svc.Create(ctx, corescope.Master(), user.CreateInput{
		Email:    *email,
		Password: secret,
		Role:     coreauth.RoleMaster,
	}, appaudit.Actor{Role: coreauth.RoleMaster, Label: "cli"})
	if err != nil {
		fmt.Fprintf(stderr, "user create-master: %v\n", err)
		return 1
	}
	// 输出只给掩码地址：完整邮箱不落 CLI 输出（与响应面同口径）。
	fmt.Fprintf(stdout, "master 已创建: id=%s email=%s\n", rec.ID, coremask.MaskEmail(rec.Email))
	return 0
}

// resolvePassword 取登录口令；三种来源互斥，必须恰好给一个。
// --password 只留给测试：它把口令写进进程 argv（/proc/<pid>/cmdline 全局可读）。
func resolvePassword(flagValue, filePath string, fromStdin bool, stdin io.Reader) (string, error) {
	given := 0
	for _, ok := range []bool{flagValue != "", filePath != "", fromStdin} {
		if ok {
			given++
		}
	}
	switch {
	case given == 0:
		return "", errors.New("--password-file、--password-stdin 或 --password 必须给一个")
	case given > 1:
		return "", errors.New("--password-file、--password-stdin、--password 互斥，只能给一个")
	case filePath != "":
		return readPasswordFile(filePath)
	case fromStdin:
		return readPasswordLine(stdin)
	default:
		return flagValue, nil
	}
}

// readPasswordFile 读 0600 普通文件里的口令（去掉行尾换行）。
// 组位或其他位非零即拒绝：同机其他用户能读到的口令不得使用。
// Windows 没有 POSIX 模式位，模式校验处显性跳过并说明。
func readPasswordFile(path string) (string, error) {
	info, err := os.Stat(path)
	if err != nil {
		return "", fmt.Errorf("--password-file: %w", err)
	}
	if !info.Mode().IsRegular() {
		return "", fmt.Errorf("--password-file: %s 不是普通文件", path)
	}
	if runtime.GOOS != "windows" && info.Mode().Perm()&0o077 != 0 {
		return "", fmt.Errorf("--password-file: %s 模式 %04o，组与其他位必须为零（chmod 600）",
			path, info.Mode().Perm())
	}
	body, err := os.ReadFile(path)
	if err != nil {
		return "", fmt.Errorf("--password-file: %w", err)
	}
	pw := strings.TrimRight(string(body), "\r\n")
	if pw == "" {
		return "", errors.New("--password-file: 口令为空")
	}
	return pw, nil
}

// readPasswordLine 从 stdin 读一行口令（去掉行尾换行）。
func readPasswordLine(stdin io.Reader) (string, error) {
	if stdin == nil {
		return "", errors.New("--password-stdin: 标准输入不可用")
	}
	line, err := bufio.NewReader(stdin).ReadString('\n')
	if err != nil && !errors.Is(err, io.EOF) {
		return "", fmt.Errorf("--password-stdin: %w", err)
	}
	pw := strings.TrimRight(line, "\r\n")
	if pw == "" {
		return "", errors.New("--password-stdin: 标准输入为空")
	}
	return pw, nil
}
