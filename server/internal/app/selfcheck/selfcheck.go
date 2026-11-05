// Package selfcheck 实现一键更新的只读自检。
//
// # 功能
// 输出 PASS/FAIL/SKIP 报告，覆盖配置、数据库 schema、外部工具、
// 目录权限、密钥指纹与 systemd 服务；同时提供人读文本与 JSON。
// 本包**不**写数据库、**不**停服务、**不**改动被检目录：
// 正常自检零写入，临时文件探针已移除；目录权限按服务用户的
// POSIX 属主/组/模式位推断。唯一的写路径是显式调用的 RecordKeys。
//
// # 归属
// app 层用例。外部依赖全部经 Deps 注入：文件系统、PATH 探测、
// 数据库探针、嵌入迁移版本、systemd 探针与时钟。
// cmd/cli 负责解析命令行/环境并装配真实实现；
// 本包**不** import adapters、**不**读环境变量、**不**调用 time.Now。
//
// # 复用
// Run、Report、Check、Deps、Options、DefaultDeps；
// State、LoadState、RecordKeys、KeyFingerprints。
//
// # 口径
// 退出码：全 PASS/SKIP 为 0，有 FAIL 为 1；用法错误由 cmd 层返回 2。
// SKIP 必须写原因；FAIL 必须写修复提示。
// 输出不得出现 DSN 口令、密钥原文与会话值：
// DSN 只打印脱敏目标，密钥只打印指纹前缀。
// 状态文件缺省 /var/lib/yiban/update-state.json，目录 0700、文件 0600（写入时）。
package selfcheck

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/fsperm"
)

// 报告与状态文件的 schema 版本。两者独立演进，改动需同批升版本。
const (
	// ReportSchemaVersion 是 --json 报告的 schema 版本。
	ReportSchemaVersion = 1
	// StateSchemaVersion 是状态文件的 schema 版本。
	StateSchemaVersion = 1
	// DefaultStatePath 是更新状态文件的缺省路径。
	DefaultStatePath = "/var/lib/yiban/update-state.json"
	// DefaultBackupDir 是一键更新备份根目录的缺省路径（与 update.sh 同源）。
	// 必须放在服务用户不可重命名的 root 独占父目录下：
	// /var/lib 属 root ⇒ yiban 换不了 /var/lib/yiban-updater；
	// 该根与 backups 都是 root:root 0700（备份含凭据密文，回滚还会执行
	// 备份里的 CLI）。本检查也按「属主 root + 调用者可写」判权。
	DefaultBackupDir = "/var/lib/yiban-updater/backups"
	// DefaultUnit 是 systemd 单元名（不带 .service 后缀）。
	DefaultUnit = "yiban"
)

// Status 是一条检查的结果。三态互斥。
type Status string

// 检查结果取值。
const (
	StatusPass Status = "PASS"
	StatusFail Status = "FAIL"
	StatusSkip Status = "SKIP"
)

// Check 是一条检查项。
type Check struct {
	// Name 是稳定标识，U2 按键名判门。
	Name string
	// Status 是 PASS/FAIL/SKIP。
	Status Status
	// Detail 是事实与数字；SKIP 时是跳过原因。
	Detail string
	// Hint 是修复提示；FAIL 时必须非空。
	Hint string
}

// BuildInfo 是报告里携带的版本身份。
type BuildInfo struct {
	Version   string
	Commit    string
	BuildTime string
}

// Report 是一次自检的汇总。
type Report struct {
	SchemaVersion int
	GeneratedAt   time.Time
	Build         BuildInfo
	Checks        []Check
}

// Passed 返回 PASS 数。
func (r Report) Passed() int { return r.count(StatusPass) }

// Failed 返回 FAIL 数。
func (r Report) Failed() int { return r.count(StatusFail) }

// Skipped 返回 SKIP 数。
func (r Report) Skipped() int { return r.count(StatusSkip) }

func (r Report) count(s Status) int {
	n := 0
	for _, c := range r.Checks {
		if c.Status == s {
			n++
		}
	}
	return n
}

// ExitCode 返回进程退出码：有 FAIL 即 1，否则 0。
func (r Report) ExitCode() int {
	if r.Failed() > 0 {
		return 1
	}
	return 0
}

// Text 渲染人读报告。FAIL 行附修复提示；SKIP 行写原因。
func (r Report) Text() string {
	var b strings.Builder
	fmt.Fprintf(&b, "selfcheck: version=%s commit=%s build_time=%s\n",
		r.Build.Version, r.Build.Commit, r.Build.BuildTime)
	for _, c := range r.Checks {
		switch c.Status {
		case StatusFail:
			fmt.Fprintf(&b, "[FAIL] %-24s %s\n       修复: %s\n", c.Name, c.Detail, c.Hint)
		case StatusSkip:
			fmt.Fprintf(&b, "[SKIP] %-24s %s\n", c.Name, c.Detail)
		default:
			fmt.Fprintf(&b, "[ OK ] %-24s %s\n", c.Name, c.Detail)
		}
	}
	fmt.Fprintf(&b, "汇总：%d 过 / %d 败 / %d 跳；退出码 %d\n",
		r.Passed(), r.Failed(), r.Skipped(), r.ExitCode())
	return b.String()
}

// DB 是自检用的只读数据库面。实现由装配层注入。
type DB interface {
	// SchemaVersion 读 goose 当前版本；版本表缺失返回 0。
	SchemaVersion(ctx context.Context) (int64, error)
	Close()
}

// ServiceProbe 探测 systemd 单元状态。
// available=false 表示本机没有可用的 systemd（容器/无 systemctl），按 SKIP 处理。
// active 只在 available=true 时有意义；state 是 systemctl 的原始状态词。
type ServiceProbe func(ctx context.Context) (available, active bool, state string)

// Deps 收敛自检的全部外供依赖。生产用 DefaultDeps 再补探针；测试逐字段注入。
type Deps struct {
	// Now 是时钟。必填。
	Now func() time.Time
	// FS 是文件系统端口。必填。
	FS FileSystem
	// LookPath 在 PATH 里找工具；nil 时 tools.* 全部 SKIP。
	LookPath func(name string) (string, error)
	// OpenDB 建只读连接（内部自行 Ping）；nil 时数据库面 SKIP。
	OpenDB func(ctx context.Context, dsn string) (DB, error)
	// EmbeddedVersion 返回编入本二进制的最高迁移号；nil 时该检查 SKIP。
	EmbeddedVersion func() (int64, error)
	// ServiceProbe 探测 systemd；nil 时服务检查 SKIP。
	ServiceProbe ServiceProbe
	// StatPermission 返回目录的属主/组/模式；false 表示平台不支持。
	StatPermission func(path string) (fsperm.Owner, bool)
	// CurrentIdentity 返回**调用者**（通常是 root）的身份，用于备份目录判权
	// （备份由更新器以 root 写入，按服务用户判权会把安全要求误判成故障）。
	// nil 或 false 时该判定 SKIP。
	CurrentIdentity func() (fsperm.Identity, bool)
	// UserIdentity 返回服务用户的 uid/gid/组；false 表示查不到。
	UserIdentity func(name string) (fsperm.Identity, bool)
	// Containerized 表示本进程在容器内：工具与宿主备份目录检查按 SKIP。
	Containerized bool
	// Build 是报告里的版本身份。
	Build BuildInfo
}

// Options 是已解析的自检输入。密钥字段是**原文**：只用于派生指纹，禁止打印。
type Options struct {
	// DSN 是 PostgreSQL 连接串（原文，禁止打印口令）。
	DSN string
	// HTTPAddr 是服务监听地址。
	HTTPAddr string
	// UIDir 是前端产物目录；空表示仅 API。
	UIDir string
	// PhotoDir 是照片根目录。
	PhotoDir string
	// CredentialKey 是 YIBAN_CREDENTIAL_KEY 原文。
	CredentialKey string
	// HashPepper 是 YIBAN_HASH_PEPPER 原文。
	HashPepper string
	// BackupDir 是迁移前备份目录。
	BackupDir string
	// StatePath 是更新状态文件。
	StatePath string
	// Unit 是 systemd 单元名；空取 DefaultUnit。
	Unit string
	// ServiceUser 是运行服务的系统用户（如 yiban）。
	// 空表示未知：目录权限检查按 SKIP 处理，不按调用者（可能是 root）判。
	ServiceUser string
}

// DefaultDeps 返回只读自检的最小依赖：真实时钟与 OS 文件系统。
// 其余探针必须由装配层补齐；缺失时对应检查显性 SKIP。
func DefaultDeps() Deps {
	return Deps{
		Now: coreclock.System{}.Now,
		FS:  OSFileSystem{},
		Build: BuildInfo{
			Version: "dev", Commit: "unknown", BuildTime: "unknown",
		},
	}
}

// Run 顺序跑全部检查并汇总。
func Run(ctx context.Context, deps Deps, opts Options) (Report, error) {
	if deps.Now == nil {
		return Report{}, errors.New("updatecheck: nil clock")
	}
	if deps.FS == nil {
		return Report{}, errors.New("updatecheck: nil filesystem")
	}
	opts = normalizeOptions(opts)
	rep := Report{
		SchemaVersion: ReportSchemaVersion,
		GeneratedAt:   deps.Now().UTC(),
		Build:         deps.Build,
	}
	rep.Checks = append(rep.Checks,
		checkConfigDSN(opts),
		checkConfigHTTPAddr(opts),
		checkConfigUIDir(deps, opts),
		checkConfigPhotoDir(deps, opts),
	)
	rep.Checks = append(rep.Checks, checkDatabase(ctx, deps, opts)...)
	rep.Checks = append(rep.Checks, checkTools(deps)...)
	rep.Checks = append(rep.Checks,
		checkFSPhotoDir(deps, opts),
		checkFSBackupDir(deps, opts),
	)
	rep.Checks = append(rep.Checks, checkKeys(deps.FS, opts)...)
	rep.Checks = append(rep.Checks, checkService(ctx, deps, opts))
	return rep, nil
}

// normalizeOptions 补齐缺省值。空路径不猜：调用方必须给出有效路径。
func normalizeOptions(opts Options) Options {
	if strings.TrimSpace(opts.BackupDir) == "" {
		opts.BackupDir = DefaultBackupDir
	}
	if strings.TrimSpace(opts.StatePath) == "" {
		opts.StatePath = DefaultStatePath
	}
	if strings.TrimSpace(opts.Unit) == "" {
		opts.Unit = DefaultUnit
	}
	opts.DSN = strings.TrimSpace(opts.DSN)
	opts.HTTPAddr = strings.TrimSpace(opts.HTTPAddr)
	opts.UIDir = strings.TrimSpace(opts.UIDir)
	opts.PhotoDir = strings.TrimSpace(opts.PhotoDir)
	opts.CredentialKey = strings.TrimSpace(opts.CredentialKey)
	opts.HashPepper = strings.TrimSpace(opts.HashPepper)
	opts.BackupDir = strings.TrimSpace(opts.BackupDir)
	opts.StatePath = strings.TrimSpace(opts.StatePath)
	opts.Unit = strings.TrimSpace(opts.Unit)
	opts.ServiceUser = strings.TrimSpace(opts.ServiceUser)
	return opts
}
