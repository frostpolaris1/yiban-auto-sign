// migrate-legacy 子命令：旧栈导出物 → 新栈一次性导入（P8-B）。
//
// 契约与裁决见 internal/adapters/legacy 包注释。部署顺序＝
// 装新栈 → 建校 → user create-master → migrate-legacy。
//
// # 口径
// 退出码：0 成功（默认档孤儿跳过含在内）；1 孤儿 strict 失败或行级错误；
// 2 用法/校验失败（缺旗、钥形状错、school/owner 不存在、导出物不全或被改）。
// 钥来源：新 KEK 只走环境变量 YIBAN_CREDENTIAL_KEY（与新栈装配方同一来源）；
// 旧钥只走环境变量 YIBAN_LEGACY_ACCOUNTS_KEY（64 位 hex），不收 argv——
// 命令行参数会进 shell 历史与 ps 输出，钥不过 argv。
package main

import (
	"context"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"strings"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/legacy"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
)

// cipherSealer 把 app/account 的 Cipher 适配成 legacy.Sealer。
// boundary 门禁止 adapters 导入 app，装配只能在 cmd 层完成。
type cipherSealer struct{ c *appaccount.Cipher }

func (s cipherSealer) Seal(schoolID, accountID string, p legacy.CredentialPayload) (legacy.CredentialOut, error) {
	cred, err := s.c.Seal(schoolID, accountID, appaccount.CredentialPayload{
		DeviceCode: p.DeviceCode,
		Password:   p.Password,
		PhoneModel: p.PhoneModel,
	})
	if err != nil {
		return legacy.CredentialOut{}, err
	}
	return legacy.CredentialOut{
		Alg: cred.Alg, KID: cred.KID, AADVersion: cred.AADVersion,
		Nonce: cred.Nonce, Ciphertext: cred.Ciphertext,
	}, nil
}

func runMigrateLegacy(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("migrate-legacy", flag.ContinueOnError)
	fs.SetOutput(stderr)
	dir := fs.String("dir", "", "导出物目录（P8-A db_export.py 产物，必填）")
	out := fs.String("out", "", "报告输出目录（reconciliation.jsonl + summary.json，必填）")
	dsnFlag := fs.String("dsn", "", "覆盖 YIBAN_DATABASE_URL")
	schoolID := fs.String("school-id", "", "目标学校 id（须已存在于新库，必填）")
	ownerID := fs.String("owner-user-id", "", "账号归属用户 id（须已存在于新库，必填）")
	strict := fs.Bool("strict", false, "存在孤儿 time_prefs 即失败（默认跳过＋计数）")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if *dir == "" || *out == "" || *schoolID == "" || *ownerID == "" {
		fmt.Fprintln(stderr, "migrate-legacy: --dir --out --school-id --owner-user-id 全部必填")
		return 2
	}
	dsn := strings.TrimSpace(*dsnFlag)
	if dsn == "" {
		v, _ := config.Env{}.Lookup("YIBAN_DATABASE_URL")
		dsn = strings.TrimSpace(v)
	}
	if dsn == "" {
		fmt.Fprintln(stderr, "migrate-legacy: YIBAN_DATABASE_URL 未设置（或用 --dsn 传入）")
		return 2
	}
	newKey, err := loadNewCredentialKey()
	if err != nil {
		fmt.Fprintf(stderr, "migrate-legacy: %v\n", err)
		return 2
	}
	envKey, _ := config.Env{}.Lookup("YIBAN_LEGACY_ACCOUNTS_KEY")
	legacyKeyHex := strings.TrimSpace(envKey)
	if legacyKeyHex == "" {
		fmt.Fprintln(stderr, "migrate-legacy: 旧钥缺失：设置环境变量 YIBAN_LEGACY_ACCOUNTS_KEY（64 位 hex）")
		return 2
	}
	legacyKey, err := hex.DecodeString(legacyKeyHex)
	if err != nil || len(legacyKey) != 32 {
		fmt.Fprintf(stderr, "migrate-legacy: 旧钥必须是 64 位 hex（32 字节），解码得 %d 字节\n", len(legacyKey))
		return 2
	}
	cipher, err := appaccount.NewCipher(newKey)
	if err != nil {
		fmt.Fprintf(stderr, "migrate-legacy: 新栈凭据钥: %v\n", err)
		return 2
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()
	if _, err := pg.Migrate(ctx, dsn); err != nil {
		fmt.Fprintf(stderr, "migrate-legacy: apply migrations: %v\n", err)
		return 1
	}
	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 4, StatementTimeout: 60 * time.Second})
	if err != nil {
		fmt.Fprintf(stderr, "migrate-legacy: open database: %v\n", err)
		return 1
	}
	defer db.Close()

	summary, code, err := legacy.Run(ctx, legacy.Options{
		Dir:         *dir,
		OutDir:      *out,
		DB:          db,
		SchoolID:    *schoolID,
		OwnerUserID: *ownerID,
		Strict:      *strict,
		LegacyKey:   legacyKey,
		Sealer:      cipherSealer{c: cipher},
		Now:         coreclock.System{}.Now().UTC(),
		Err:         stderr,
	})
	if err != nil {
		fmt.Fprintf(stderr, "%v\n", err)
		code := 2
		var fatalErr *legacy.Error
		if errors.As(err, &fatalErr) {
			code = fatalErr.Code
		}
		return code
	}
	encoded, encErr := json.MarshalIndent(summary, "", "  ")
	if encErr != nil {
		fmt.Fprintf(stderr, "migrate-legacy: encode summary: %v\n", encErr)
		return 1
	}
	fmt.Fprintln(stdout, string(encoded))
	fmt.Fprintf(stdout, "migrate-legacy: 完成，报告在 %s/（reconciliation.jsonl + summary.json）\n", *out)
	return code
}

// loadNewCredentialKey 读新栈 KEK。与装配方同一来源（YIBAN_CREDENTIAL_KEY，
// base64 的 32 字节），解码规则与 adapters/config 同口径。
func loadNewCredentialKey() ([]byte, error) {
	raw, ok := config.Env{}.Lookup("YIBAN_CREDENTIAL_KEY")
	if !ok || strings.TrimSpace(raw) == "" {
		return nil, fmt.Errorf("YIBAN_CREDENTIAL_KEY 未设置（base64 of 32 bytes）")
	}
	key, err := base64.StdEncoding.DecodeString(strings.TrimSpace(raw))
	if err != nil {
		return nil, fmt.Errorf("YIBAN_CREDENTIAL_KEY 不是合法 base64")
	}
	if len(key) != 32 {
		return nil, fmt.Errorf("YIBAN_CREDENTIAL_KEY 解出 %d 字节，须为 32 字节", len(key))
	}
	return key, nil
}
