package main

// user 子命令：`user create-master` 是干净部署建第一个 master 的
// 唯一自助入口（P7 审查 P2-1：没有它，部署完成也无人能登录）。
// 之后的所有建号都走已认证的 POST /api/users。

import (
	"context"
	"flag"
	"fmt"
	"io"
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
func runUser(args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 || args[0] != "create-master" {
		fmt.Fprintln(stderr, `用法: yiban-cli user create-master --email <地址> --password <口令> [--dsn <串>] [--pepper <串>]
子命令只有 create-master；后续建号走已认证的 POST /api/users。`)
		return 2
	}
	fs := flag.NewFlagSet("user create-master", flag.ContinueOnError)
	fs.SetOutput(stderr)
	email := fs.String("email", "", "master 登录地址")
	password := fs.String("password", "", "登录口令")
	dsn := fs.String("dsn", "", "覆盖 YIBAN_DATABASE_URL")
	pepper := fs.String("pepper", "", "覆盖 YIBAN_HASH_PEPPER（至少 16 字符）")
	if err := fs.Parse(args[1:]); err != nil {
		return 2
	}
	if strings.TrimSpace(*email) == "" || *password == "" {
		fmt.Fprintln(stderr, "user create-master: --email 与 --password 必填")
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
		Limit: assembly.UserCapacityLimit,
	})
	if err != nil {
		fmt.Fprintf(stderr, "user create-master: %v\n", err)
		return 1
	}
	// 首个 master 没有会话：审计主体记 cli（无邮箱形状，满足 actor_label CHECK）。
	rec, err := svc.Create(ctx, corescope.Master(), user.CreateInput{
		Email:    *email,
		Password: *password,
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
