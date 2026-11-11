// selfcheck 子命令：一键更新的只读自检。
//
// # 功能
// 装配生产探针（真实环境变量、PATH、PostgreSQL、systemd），
// 调 internal/app/selfcheck 生成人读报告或 JSON。
// 本子命令**不**写数据库、**不**停服务、**不**改被检目录；
// 唯一写路径是显式 --record-keys（更新成功后记录指纹）。
//
// # 归属
// cmd/cli 薄装配层。检查判据全在 app 层；本文件只做参数解析、
// 缺省值（与 config 常量同源）与真实实现注入。
//
// # 口径
// 退出码：0 全 PASS/SKIP；1 有 FAIL 或运行错误；2 用法错误。
// 密钥原文没有命令行参数：避免出现在 ps/argv 与 shell 历史里，只从环境读。
// 输出不回显 DSN 口令与密钥原文。
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app/selfcheck"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/buildinfo"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/fsperm"
)

// selfcheckLookup 读取进程环境。环境读点仍收敛在 adapters/config。
func selfcheckLookup(key string) string {
	v, _ := config.Env{}.Lookup(key)
	return strings.TrimSpace(v)
}

// newSelfcheckDeps 装配生产依赖：PATH、数据库探针、嵌入迁移版本、systemd、
// 服务用户的 POSIX 权限探针。目录判权复用 doctor 的 dirStatOf/userIdentityOf。
func newSelfcheckDeps() selfcheck.Deps {
	deps := selfcheck.DefaultDeps()
	deps.FS = osFileSystem{}
	deps.LookPath = exec.LookPath
	deps.OpenDB = openSelfcheckDB
	deps.EmbeddedVersion = pg.EmbeddedVersion
	deps.ServiceProbe = systemdServiceProbe
	deps.StatPermission = statPermissionOf
	deps.CurrentIdentity = currentIdentityOf
	deps.UserIdentity = userIdentityOf
	deps.Containerized = inContainer()
	info := buildinfo.Current()
	deps.Build = selfcheck.BuildInfo{
		Version:   info.Version,
		Commit:    info.Commit,
		BuildTime: info.BuildTime,
	}
	return deps
}

// statPermissionOf 把 doctor 的目录 stat 转成 fsperm 判据输入。
func statPermissionOf(path string) (fsperm.Owner, bool) {
	info, ok := dirStatOf(path)
	if !ok {
		return fsperm.Owner{}, false
	}
	return fsperm.Owner{
		UID: info.uid, GID: info.gid,
		OwnerName: info.owner, GroupName: info.group,
		Mode: info.perm, HasIDs: info.hasIDs,
	}, true
}

// systemdServiceUserFromUnit 从单元文件取运行用户。
// 无 systemctl、无 systemd 或命令失败都返回空串：调用方按未知处理并 SKIP。
func systemdServiceUserFromUnit() string {
	if _, err := exec.LookPath("systemctl"); err != nil {
		return ""
	}
	if _, err := os.Stat("/run/systemd/system"); err != nil {
		return ""
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, "systemctl",
		"show", selfcheck.DefaultUnit+".service", "-p", "User", "--value").Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}

// openSelfcheckDB 复用 doctor 的只读探针（pgHandle 已实现 SchemaVersion）。
// pepper 传 nil：自检不校验审计链，只读 schema 版本。
func openSelfcheckDB(ctx context.Context, dsn string) (selfcheck.DB, error) {
	handle, err := openPgProbe(ctx, dsn, nil)
	if err != nil {
		return nil, err
	}
	if handle == nil {
		return nil, errors.New("selfcheck: 数据库探针未返回句柄")
	}
	return handle, nil
}

// systemdServiceProbe 跑 systemctl is-active。
// 没有 systemctl 或 /run/systemd/system（容器）时报 available=false，按 SKIP 处理。
func systemdServiceProbe(ctx context.Context) (available, active bool, state string) {
	if _, err := exec.LookPath("systemctl"); err != nil {
		return false, false, ""
	}
	if _, err := os.Stat("/run/systemd/system"); err != nil {
		return false, false, ""
	}
	out, err := exec.CommandContext(ctx, "systemctl", "is-active", selfcheck.DefaultUnit).Output()
	state = strings.TrimSpace(string(out))
	if err != nil {
		if state == "" {
			state = "unknown"
		}
		return true, false, state
	}
	return true, state == "active", state
}

// runSelfcheck 是 selfcheck 子命令入口。
func runSelfcheck(args []string, stdout, stderr io.Writer) int {
	return runSelfcheckWith(args, stdout, stderr, selfcheckLookup, newSelfcheckDeps(), systemdServiceUserFromUnit)
}

// runSelfcheckWith 是测试接缝：lookup、deps 与服务用户缺省值都可注入。
func runSelfcheckWith(args []string, stdout, stderr io.Writer, lookup func(string) string, deps selfcheck.Deps, serviceUserDefault func() string) int {
	fs := newFlagSet("selfcheck", stderr)
	dsn := fs.String("dsn", "", "覆盖 YIBAN_DATABASE_URL")
	httpAddr := fs.String("http-addr", "", "覆盖 YIBAN_HTTP_ADDR")
	uiDir := fs.String("ui-dir", "", "覆盖 YIBAN_UI_DIR")
	photoDir := fs.String("photo-dir", "", "覆盖 YIBAN_PHOTO_DIR")
	backupDir := fs.String("backup-dir", "", "更新备份根目录（缺省 "+selfcheck.DefaultBackupDir+"）")
	statePath := fs.String("state", "", "更新状态文件（缺省 "+selfcheck.DefaultStatePath+"）")
	serviceUser := fs.String("service-user", "",
		"运行服务的系统用户（缺省 systemctl show "+selfcheck.DefaultUnit+".service -p User --value）")
	asJSON := fs.Bool("json", false, "输出 JSON 报告（不混人读行）")
	recordKeys := fs.Bool("record-keys", false, "把当前密钥指纹写入状态文件（更新成功后调用）")
	timeout := fs.Duration("timeout", 15*time.Second, "整体超时")
	fs.Usage = func() { selfcheckUsage(fs, stderr) }
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if fs.NArg() > 0 {
		_, _ = fmt.Fprintf(stderr, "selfcheck: 不认识的参数 %q\n", strings.Join(fs.Args(), " "))
		selfcheckUsage(fs, stderr)
		return 2
	}
	if *asJSON && *recordKeys {
		_, _ = fmt.Fprintln(stderr, "selfcheck: --json 与 --record-keys 不能同时使用")
		return 2
	}
	if *timeout <= 0 {
		_, _ = fmt.Fprintln(stderr, "selfcheck: --timeout 必须大于 0")
		return 2
	}
	svcUser := strings.TrimSpace(*serviceUser)
	if svcUser == "" && serviceUserDefault != nil {
		svcUser = strings.TrimSpace(serviceUserDefault())
	}

	opts := selfcheck.Options{
		DSN:           firstNonEmpty(*dsn, lookup("YIBAN_DATABASE_URL")),
		HTTPAddr:      firstNonEmpty(*httpAddr, lookup("YIBAN_HTTP_ADDR"), config.DefaultHTTPAddr),
		UIDir:         firstNonEmpty(*uiDir, lookup("YIBAN_UI_DIR")),
		PhotoDir:      firstNonEmpty(*photoDir, lookup("YIBAN_PHOTO_DIR"), config.DefaultPhotoDir),
		CredentialKey: lookup("YIBAN_CREDENTIAL_KEY"),
		HashPepper:    lookup("YIBAN_HASH_PEPPER"),
		BackupDir:     firstNonEmpty(*backupDir, selfcheck.DefaultBackupDir),
		StatePath:     firstNonEmpty(*statePath, selfcheck.DefaultStatePath),
		Unit:          selfcheck.DefaultUnit,
		ServiceUser:   svcUser,
	}

	if *recordKeys {
		state, err := selfcheck.RecordKeys(deps.FS, opts.StatePath, opts, deps.Now())
		if err != nil {
			_, _ = fmt.Fprintf(stderr, "selfcheck: 记录密钥指纹失败: %v\n", err)
			return 1
		}
		_, _ = fmt.Fprintf(stdout, "已记录密钥指纹到 %s\ncredential_key=%s\nhash_pepper=%s\nrecorded_at=%s\n",
			opts.StatePath,
			selfcheck.ShortFingerprint(state.KeyFingerprints[selfcheck.FingerprintCredentialKey]),
			selfcheck.ShortFingerprint(state.KeyFingerprints[selfcheck.FingerprintHashPepper]),
			state.RecordedAt)
		return 0
	}

	ctx, cancel := context.WithTimeout(context.Background(), *timeout)
	defer cancel()
	rep, err := selfcheck.Run(ctx, deps, opts)
	if err != nil {
		_, _ = fmt.Fprintf(stderr, "selfcheck: %v\n", err)
		return 1
	}
	if *asJSON {
		body, err := rep.JSON()
		if err != nil {
			_, _ = fmt.Fprintf(stderr, "selfcheck: 编码 JSON: %v\n", err)
			return 1
		}
		_, _ = stdout.Write(body)
		return rep.ExitCode()
	}
	_, _ = io.WriteString(stdout, rep.Text())
	return rep.ExitCode()
}

// firstNonEmpty 返回第一个去空白后非空的值。
// 用于「命令行 > 环境 > 缺省」的优先级；空字符串不参与。
func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if strings.TrimSpace(v) != "" {
			return strings.TrimSpace(v)
		}
	}
	return ""
}

// selfcheckUsage 打印分节帮助。
func selfcheckUsage(fs *flag.FlagSet, w io.Writer) {
	_, _ = fmt.Fprintf(w, `用法: yiban-cli selfcheck [选项]

只读自检：不写数据库、不停服务、不改被检目录。
正常自检零写入；临时文件探针已移除。
目录权限按 --service-user 的 POSIX 属主/组/模式位推断。
除 --record-keys 外不写任何文件。
退出码：全 PASS/SKIP 为 0；有 FAIL 为 1；用法错误 2。

选项:
`)
	fs.SetOutput(w)
	fs.PrintDefaults()
	_, _ = fmt.Fprintf(w, `
检查面（每条 PASS/FAIL/SKIP 带原因；JSON 里 name 稳定）:
  配置      config.dsn config.http_addr config.ui_dir config.photo_dir
  数据库    db.connect db.schema_version db.migrations_embedded db.migrations_pending
  工具      tools.pg_dump tools.pg_restore tools.psql tools.gpg tools.tar
  文件系统  fs.photo_dir fs.backup_dir
  密钥      keys.credential_key keys.hash_pepper keys.state
  服务      service.yiban

--record-keys 是唯一写路径：更新成功后把当前密钥指纹写入 --state，
供下一次自检比对。密钥原文只从环境读，没有命令行参数。
`)
}
