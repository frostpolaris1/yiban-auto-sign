// selfcheck 的逐项检查。
//
// # 口径
// 每条检查只做只读动作：SELECT、stat/readdir、PATH 探测、systemctl is-active。
// 数据库检查共用一次连接（自检是低频运维动作，但没必要开四次池）。
// 任一 FAIL 必须给修复提示；任一 SKIP 必须写原因。
package updatecheck

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"strconv"
	"strings"
)

// 检查项名。U2 按键名判门，改名即接口变更。
const (
	nameConfigDSN       = "config.dsn"
	nameConfigHTTPAddr  = "config.http_addr"
	nameConfigUIDir     = "config.ui_dir"
	nameConfigPhotoDir  = "config.photo_dir"
	nameDBConnect       = "db.connect"
	nameDBSchemaVersion = "db.schema_version"
	nameDBEmbedded      = "db.migrations_embedded"
	nameDBPending       = "db.migrations_pending"
	nameFSPhotoDir      = "fs.photo_dir"
	nameFSBackupDir     = "fs.backup_dir"
	nameKeysCredential  = "keys.credential_key"
	nameKeysPepper      = "keys.hash_pepper"
	nameKeysState       = "keys.state"
	nameService         = "service.yiban"
)

// tool 是一个外部命令与缺失时的修复提示。
type tool struct {
	name string
	hint string
}

// requiredTools 是更新链路依赖的外部命令。
var requiredTools = []tool{
	{"pg_dump", "安装 PostgreSQL 客户端（Debian/Ubuntu: apt install postgresql-client）"},
	{"pg_restore", "安装 PostgreSQL 客户端（Debian/Ubuntu: apt install postgresql-client）"},
	{"psql", "安装 PostgreSQL 客户端（Debian/Ubuntu: apt install postgresql-client）"},
	{"gpg", "安装 GnuPG（Debian/Ubuntu: apt install gnupg）"},
	{"tar", "安装 tar（Debian/Ubuntu: apt install tar）"},
}

// pass / fail / skip 是三种结果的构造器，保证 FAIL 带提示、SKIP 带原因。
func pass(name, detail string) Check {
	return Check{Name: name, Status: StatusPass, Detail: detail}
}

func fail(name, detail, hint string) Check {
	return Check{Name: name, Status: StatusFail, Detail: detail, Hint: hint}
}

func skip(name, reason string) Check {
	return Check{Name: name, Status: StatusSkip, Detail: reason}
}

// checkConfigDSN 检查 DSN 已设置且形状可用；只打印脱敏目标。
func checkConfigDSN(opts Options) Check {
	if opts.DSN == "" {
		return fail(nameConfigDSN, "YIBAN_DATABASE_URL 未设置（也可用 --dsn 传入）",
			`在 /etc/yiban/.env 设置 YIBAN_DATABASE_URL=postgres://用户:口令@主机:5432/库?sslmode=disable`)
	}
	target, err := dsnTarget(opts.DSN)
	if err != nil {
		return fail(nameConfigDSN, "DSN 形状无法使用："+err.Error()+"（原文与口令未打印）",
			"改用 postgres:// 或 key=value 形态的 DSN")
	}
	return pass(nameConfigDSN, "已设置；目标 "+target)
}

// checkConfigHTTPAddr 检查监听地址是 host:port。
func checkConfigHTTPAddr(opts Options) Check {
	if opts.HTTPAddr == "" {
		return fail(nameConfigHTTPAddr, "YIBAN_HTTP_ADDR 未设置",
			"设置 YIBAN_HTTP_ADDR=127.0.0.1:47183")
	}
	_, port, err := net.SplitHostPort(opts.HTTPAddr)
	if err != nil {
		return fail(nameConfigHTTPAddr,
			fmt.Sprintf("%q 不是 host:port 形状", opts.HTTPAddr),
			"写成 127.0.0.1:47183 这样的 host:port")
	}
	n, err := strconv.Atoi(port)
	if err != nil || n < 1 || n > 65535 {
		return fail(nameConfigHTTPAddr,
			fmt.Sprintf("端口 %q 不是 1–65535 的整数", port),
			"改成 1–65535 的端口号")
	}
	return pass(nameConfigHTTPAddr, "值 "+opts.HTTPAddr)
}

// checkConfigUIDir 检查前端目录存在、可读且含 index.html。
// 未配置是合法形态（仅 API 部署），按 SKIP 显性说明。
func checkConfigUIDir(fsys FileSystem, opts Options) Check {
	if opts.UIDir == "" {
		return skip(nameConfigUIDir, "未配置 YIBAN_UI_DIR（仅 API 部署）")
	}
	info, err := fsys.Stat(opts.UIDir)
	switch {
	case errors.Is(err, os.ErrNotExist):
		return fail(nameConfigUIDir, "目录不存在："+opts.UIDir,
			"检查 YIBAN_UI_DIR；页面未安装时先跑 install.sh --ui-dir")
	case err != nil:
		return fail(nameConfigUIDir, "无法读取目录 "+opts.UIDir+"："+err.Error(),
			"检查路径与进程权限")
	case !info.IsDir():
		return fail(nameConfigUIDir, "不是目录："+opts.UIDir,
			"YIBAN_UI_DIR 必须指向前端构建产物目录")
	}
	entries, err := fsys.ReadDir(opts.UIDir)
	if err != nil {
		return fail(nameConfigUIDir, "目录不可读 "+opts.UIDir+"："+err.Error(),
			"检查目录权限（页面目录应对服务用户可读）")
	}
	for _, entry := range entries {
		if entry.Name() == "index.html" && !entry.IsDir() {
			return pass(nameConfigUIDir, "可读，含 index.html："+opts.UIDir)
		}
	}
	return fail(nameConfigUIDir, "缺 index.html："+opts.UIDir,
		"重新构建前端并把 dist 目录作为 --ui-dir 安装")
}

// checkConfigPhotoDir 检查照片目录已配置且可读。目录不存在留待首次启动创建。
func checkConfigPhotoDir(fsys FileSystem, opts Options) Check {
	if opts.PhotoDir == "" {
		return fail(nameConfigPhotoDir, "YIBAN_PHOTO_DIR 为空",
			"设置 YIBAN_PHOTO_DIR=/var/lib/yiban/photos")
	}
	info, err := fsys.Stat(opts.PhotoDir)
	switch {
	case errors.Is(err, os.ErrNotExist):
		return skip(nameConfigPhotoDir,
			"配置为 "+opts.PhotoDir+"，目录尚不存在（首次运行会创建）")
	case err != nil:
		return fail(nameConfigPhotoDir, "无法读取目录 "+opts.PhotoDir+"："+err.Error(),
			"检查路径与进程权限")
	case !info.IsDir():
		return fail(nameConfigPhotoDir, "不是目录："+opts.PhotoDir,
			"YIBAN_PHOTO_DIR 必须是目录")
	}
	if err := fsys.ProbeReadable(opts.PhotoDir); err != nil {
		return fail(nameConfigPhotoDir, "目录不可读 "+opts.PhotoDir+"："+err.Error(),
			"chown -R yiban:yiban "+opts.PhotoDir)
	}
	return pass(nameConfigPhotoDir, "存在且可读："+opts.PhotoDir)
}

// checkDatabase 是数据库面四行：连接、当前版本、嵌入版本、待执行迁移。
// 只读：SELECT 与连接 Ping，不写任何行。
func checkDatabase(ctx context.Context, deps Deps, opts Options) []Check {
	rows := make([]Check, 0, 4)
	var db DB
	connected := false
	switch {
	case opts.DSN == "":
		rows = append(rows, skip(nameDBConnect, "前置 config.dsn 未通过（YIBAN_DATABASE_URL 未设置）"))
	case deps.OpenDB == nil:
		rows = append(rows, skip(nameDBConnect, "数据库探针未注入"))
	default:
		handle, err := deps.OpenDB(ctx, opts.DSN)
		if err != nil {
			if handle != nil {
				handle.Close()
			}
			rows = append(rows, fail(nameDBConnect,
				"连接失败："+sanitizeText(err.Error(), opts.DSN),
				`psql "你的DSN" -c 'select 1' 核对主机/端口/口令/库名（本报告不回显口令）`))
		} else {
			db = handle
			connected = true
			defer db.Close()
			rows = append(rows, pass(nameDBConnect, "连接成功"))
		}
	}

	var current int64
	schemaOK := false
	if !connected {
		rows = append(rows, skip(nameDBSchemaVersion, "前置 db.connect 未通过"))
	} else {
		version, err := db.SchemaVersion(ctx)
		if err != nil {
			rows = append(rows, fail(nameDBSchemaVersion,
				"读取 goose schema 版本失败："+sanitizeText(err.Error(), opts.DSN),
				"用 psql 检查 goose_db_version 与当前角色权限"))
		} else {
			current = version
			schemaOK = true
			detail := fmt.Sprintf("当前 goose 版本 %d", version)
			if version == 0 {
				detail = "当前 goose 版本 0（无版本表：尚未迁移）"
			}
			rows = append(rows, pass(nameDBSchemaVersion, detail))
		}
	}

	var embedded int64
	embeddedOK := false
	switch {
	case deps.EmbeddedVersion == nil:
		rows = append(rows, skip(nameDBEmbedded, "嵌入迁移版本未注入"))
	default:
		version, err := deps.EmbeddedVersion()
		switch {
		case err != nil:
			rows = append(rows, fail(nameDBEmbedded,
				"读取嵌入迁移版本失败："+err.Error(),
				"二进制缺 migrations 嵌入；重新构建"))
		case version <= 0:
			rows = append(rows, fail(nameDBEmbedded,
				"嵌入迁移版本为 0（迁移集为空）",
				"检查 server/migrations/postgres 是否有 SQL 文件后重建"))
		default:
			embedded = version
			embeddedOK = true
			rows = append(rows, pass(nameDBEmbedded, fmt.Sprintf("嵌入最高迁移 %d", version)))
		}
	}

	switch {
	case !schemaOK || !embeddedOK:
		rows = append(rows, skip(nameDBPending,
			"前置 db.schema_version / db.migrations_embedded 未通过"))
	case current < embedded:
		rows = append(rows, fail(nameDBPending,
			fmt.Sprintf("有待执行迁移：当前 %d，嵌入 %d（差 %d）", current, embedded, embedded-current),
			"yiban-cli migrate（U2 在停机窗口内执行）"))
	case current > embedded:
		rows = append(rows, fail(nameDBPending,
			fmt.Sprintf("数据库超前：当前 %d > 嵌入 %d；禁止降级", current, embedded),
			"换用与数据库 schema 匹配的新版二进制"))
	default:
		rows = append(rows, pass(nameDBPending, fmt.Sprintf("无待执行迁移（%d）", current)))
	}
	return rows
}

// checkTools 逐个探测更新链路的外部命令。
func checkTools(deps Deps) []Check {
	rows := make([]Check, 0, len(requiredTools))
	for _, t := range requiredTools {
		name := "tools." + t.name
		if deps.LookPath == nil {
			rows = append(rows, skip(name, "PATH 探测未注入"))
			continue
		}
		path, err := deps.LookPath(t.name)
		if err != nil {
			rows = append(rows, fail(name, t.name+" 未在 PATH 找到", t.hint))
			continue
		}
		rows = append(rows, pass(name, "找到 "+path))
	}
	return rows
}

// checkFSPhotoDir 检查照片目录可写（只在目录存在时检查）。
func checkFSPhotoDir(fsys FileSystem, opts Options) Check {
	if opts.PhotoDir == "" {
		return fail(nameFSPhotoDir, "YIBAN_PHOTO_DIR 为空",
			"设置 YIBAN_PHOTO_DIR=/var/lib/yiban/photos")
	}
	info, err := fsys.Stat(opts.PhotoDir)
	switch {
	case errors.Is(err, os.ErrNotExist):
		return skip(nameFSPhotoDir, "目录不存在，跳过写检查："+opts.PhotoDir)
	case err != nil:
		return fail(nameFSPhotoDir, "无法读取目录 "+opts.PhotoDir+"："+err.Error(),
			"检查路径与进程权限")
	case !info.IsDir():
		return fail(nameFSPhotoDir, "不是目录："+opts.PhotoDir,
			"YIBAN_PHOTO_DIR 必须是目录")
	}
	if err := fsys.ProbeWritable(opts.PhotoDir); err != nil {
		return fail(nameFSPhotoDir, "目录不可写 "+opts.PhotoDir+"："+err.Error(),
			"chown -R yiban:yiban "+opts.PhotoDir)
	}
	return pass(nameFSPhotoDir, "可写："+opts.PhotoDir)
}

// checkFSBackupDir 检查迁移前备份目录可写。
// 目录不存在时看最近的已存在祖先：可写则 SKIP（U2 备份前创建），否则 FAIL。
func checkFSBackupDir(fsys FileSystem, opts Options) Check {
	dir := opts.BackupDir
	if dir == "" {
		return fail(nameFSBackupDir, "备份目录为空",
			"用 --backup-dir 指定目录（缺省 "+DefaultBackupDir+"）")
	}
	info, err := fsys.Stat(dir)
	if err == nil {
		if !info.IsDir() {
			return fail(nameFSBackupDir, "不是目录："+dir,
				"换一个目录或删除同名文件")
		}
		if err := fsys.ProbeWritable(dir); err != nil {
			return fail(nameFSBackupDir, "目录不可写 "+dir+"："+err.Error(),
				"chmod 700 "+dir+" 或 chown 给运行更新的用户")
		}
		return pass(nameFSBackupDir, "可写："+dir)
	}
	if !errors.Is(err, os.ErrNotExist) {
		return fail(nameFSBackupDir, "无法读取目录 "+dir+"："+err.Error(),
			"检查路径与进程权限")
	}
	return missingBackupDirRow(fsys, dir)
}

// missingBackupDirRow 沿父目录上溯，找最近的可写祖先。
// 找不到可写祖先即 FAIL：更新在停机后才发现备份不了，代价更大。
func missingBackupDirRow(fsys FileSystem, dir string) Check {
	ancestor := parentDir(dir)
	for {
		info, err := fsys.Stat(ancestor)
		switch {
		case err == nil:
			if !info.IsDir() {
				return fail(nameFSBackupDir, "不存在，且最近路径 "+ancestor+" 不是目录",
					"换一个备份目录")
			}
			if err := fsys.ProbeWritable(ancestor); err != nil {
				return fail(nameFSBackupDir,
					"不存在，且最近的已存在目录 "+ancestor+" 不可写："+err.Error(),
					"用 --backup-dir 指向可写目录，或先创建并 chown")
			}
			return skip(nameFSBackupDir,
				"不存在；"+ancestor+" 可写，U2 备份前会创建 "+dir)
		case !errors.Is(err, os.ErrNotExist):
			return fail(nameFSBackupDir, "无法读取 "+ancestor+"："+err.Error(),
				"检查路径与进程权限")
		}
		parent := parentDir(ancestor)
		if parent == ancestor {
			return fail(nameFSBackupDir, "不存在且找不到可写的父目录："+dir,
				"用 --backup-dir 指向可写目录")
		}
		ancestor = parent
	}
}

// parentDir 取父目录，同时接受 / 与 \ 分隔符：
// 生产是 Linux 路径，测试与开发机可能是 Windows 路径。
func parentDir(p string) string {
	for i := len(p) - 1; i >= 0; i-- {
		if p[i] == '/' || p[i] == '\\' {
			if i == 0 {
				return p[:1]
			}
			return p[:i]
		}
	}
	return "."
}

// checkKeys 是密钥面三行：两枚指纹 + 与状态文件比对。
func checkKeys(fsys FileSystem, opts Options) []Check {
	credentialFP, credentialErr := CredentialKeyFingerprint(opts.CredentialKey)
	pepperFP, pepperErr := HashPepperFingerprint(opts.HashPepper)
	rows := make([]Check, 0, 3)
	if credentialErr != nil {
		rows = append(rows, fail(nameKeysCredential, credentialErr.Error(),
			`openssl rand -base64 32 生成后写入 YIBAN_CREDENTIAL_KEY`))
	} else {
		rows = append(rows, pass(nameKeysCredential, "指纹 "+ShortFingerprint(credentialFP)))
	}
	if pepperErr != nil {
		rows = append(rows, fail(nameKeysPepper, pepperErr.Error(),
			`openssl rand -hex 32 生成后写入 YIBAN_HASH_PEPPER（轮换会让审计链失效）`))
	} else {
		rows = append(rows, pass(nameKeysPepper, "指纹 "+ShortFingerprint(pepperFP)))
	}
	rows = append(rows, checkKeyState(fsys, opts, credentialFP, credentialErr, pepperFP, pepperErr))
	return rows
}

// checkKeyState 比对状态文件里的指纹。状态文件缺失不是绿：显性 SKIP 并点名记录命令。
func checkKeyState(fsys FileSystem, opts Options, credentialFP string, credentialErr error, pepperFP string, pepperErr error) Check {
	st, exists, err := LoadState(fsys, opts.StatePath)
	if err != nil {
		return fail(nameKeysState, err.Error(),
			"检查/修复状态文件 JSON；确认无误后用 selfcheck --record-keys 重建")
	}
	if !exists {
		return skip(nameKeysState,
			"状态文件不存在："+opts.StatePath+"（首次运行；更新成功后用 selfcheck --record-keys 记录）")
	}
	if credentialErr != nil || pepperErr != nil {
		return skip(nameKeysState, "前置密钥检查未通过")
	}
	var changed []string
	want := map[string]string{
		FingerprintCredentialKey: credentialFP,
		FingerprintHashPepper:    pepperFP,
	}
	for _, name := range []string{FingerprintCredentialKey, FingerprintHashPepper} {
		recorded, ok := st.KeyFingerprints[name]
		switch {
		case !ok || recorded == "":
			changed = append(changed, name+" 在记录里缺失")
		case recorded != want[name]:
			changed = append(changed,
				fmt.Sprintf("%s 指纹变化（记录 %s，当前 %s）",
					name, ShortFingerprint(recorded), ShortFingerprint(want[name])))
		}
	}
	if len(changed) > 0 {
		return fail(nameKeysState, strings.Join(changed, "；"),
			"更新不改密钥：先确认轮换是否为有意操作；确认后重跑 selfcheck --record-keys")
	}
	recordedAt := st.RecordedAt
	if recordedAt == "" {
		recordedAt = "unknown"
	}
	return pass(nameKeysState, "两枚指纹与记录一致（recorded_at "+recordedAt+"）")
}

// checkService 探测 systemd 单元。无 systemd（容器）即 SKIP，不算绿也不拦更新。
func checkService(ctx context.Context, deps Deps, opts Options) Check {
	unit := opts.Unit + ".service"
	if deps.ServiceProbe == nil {
		return skip(nameService, "systemd 探测未注入")
	}
	available, active, state := deps.ServiceProbe(ctx)
	if !available {
		return skip(nameService, "systemd 不可用（容器或未安装 systemctl）")
	}
	if active {
		return pass(nameService, unit+" active")
	}
	if state == "" {
		state = "unknown"
	}
	return fail(nameService, unit+" 状态 "+state,
		"systemctl start "+opts.Unit+"（升级前服务应在运行）")
}
