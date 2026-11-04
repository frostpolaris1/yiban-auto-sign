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

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/fsperm"
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

// checkConfigUIDir 检查前端目录存在、含 index.html，且服务用户可读。
// 未配置是合法形态（仅 API 部署），按 SKIP 显性说明。
func checkConfigUIDir(deps Deps, opts Options) Check {
	if opts.UIDir == "" {
		return skip(nameConfigUIDir, "未配置 YIBAN_UI_DIR（仅 API 部署）")
	}
	info, err := deps.FS.Stat(opts.UIDir)
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
	entries, err := deps.FS.ReadDir(opts.UIDir)
	if err != nil {
		return fail(nameConfigUIDir, "目录不可读 "+opts.UIDir+"："+err.Error(),
			"检查目录权限（页面目录应对服务用户可读）")
	}
	hasIndex := false
	for _, entry := range entries {
		if entry.Name() == "index.html" && !entry.IsDir() {
			hasIndex = true
			break
		}
	}
	if !hasIndex {
		return fail(nameConfigUIDir, "缺 index.html："+opts.UIDir,
			"重新构建前端并把 dist 目录作为 --ui-dir 安装")
	}
	verdict := judgeDirAccess(deps, opts, opts.UIDir, fsperm.Read|fsperm.Execute, "读")
	if verdict.skip != "" {
		return skip(nameConfigUIDir, "目录含 index.html；"+verdict.skip)
	}
	if !verdict.allowed {
		return fail(nameConfigUIDir, verdict.detail+"；目录含 index.html",
			"chown -R "+opts.ServiceUser+":"+opts.ServiceUser+" "+opts.UIDir+"（页面目录应对服务用户可读）")
	}
	return pass(nameConfigUIDir, "含 index.html；"+verdict.detail)
}

// checkConfigPhotoDir 检查照片目录已配置、存在且服务用户可读。
// 目录不存在留待首次启动创建；可判定性按服务用户，不按调用者（可能是 root）。
func checkConfigPhotoDir(deps Deps, opts Options) Check {
	if opts.PhotoDir == "" {
		return fail(nameConfigPhotoDir, "YIBAN_PHOTO_DIR 为空",
			"设置 YIBAN_PHOTO_DIR=/var/lib/yiban/photos")
	}
	info, err := deps.FS.Stat(opts.PhotoDir)
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
	verdict := judgeDirAccess(deps, opts, opts.PhotoDir, fsperm.Read|fsperm.Execute, "读")
	if verdict.skip != "" {
		return skip(nameConfigPhotoDir, verdict.skip)
	}
	if !verdict.allowed {
		return fail(nameConfigPhotoDir, verdict.detail,
			"chown -R "+opts.ServiceUser+":"+opts.ServiceUser+" "+opts.PhotoDir)
	}
	return pass(nameConfigPhotoDir, verdict.detail)
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
	switch deps.EmbeddedVersion {
	case nil:
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
// 容器（distroless）内没有这些工具：SKIP 并指向宿主，不装绿也不误报。
func checkTools(deps Deps) []Check {
	rows := make([]Check, 0, len(requiredTools))
	for _, t := range requiredTools {
		name := "tools." + t.name
		if deps.Containerized {
			rows = append(rows, skip(name,
				"容器路径请在宿主运行 selfcheck（distroless 无 "+t.name+" 等工具）"))
			continue
		}
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

// checkFSPhotoDir 检查照片目录对服务用户可写（只在目录存在时检查）。
func checkFSPhotoDir(deps Deps, opts Options) Check {
	if opts.PhotoDir == "" {
		return fail(nameFSPhotoDir, "YIBAN_PHOTO_DIR 为空",
			"设置 YIBAN_PHOTO_DIR=/var/lib/yiban/photos")
	}
	info, err := deps.FS.Stat(opts.PhotoDir)
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
	verdict := judgeDirAccess(deps, opts, opts.PhotoDir, fsperm.Write|fsperm.Execute, "写")
	if verdict.skip != "" {
		return skip(nameFSPhotoDir, verdict.skip)
	}
	if !verdict.allowed {
		return fail(nameFSPhotoDir, verdict.detail,
			"chown -R "+opts.ServiceUser+":"+opts.ServiceUser+" "+opts.PhotoDir)
	}
	return pass(nameFSPhotoDir, verdict.detail)
}

// checkFSBackupDir 检查备份目录对**调用者**（root）可写。
// 备份由 update.sh 以 root 写入，目录也是 root:root 0700：按服务用户判权
// 会得出「服务用户写不了 ⇒ FAIL」，而那正是安全要求（服务用户不得能塞件）。
// 容器内跳过：备份目录在宿主；裸机目录不存在时看最近的已存在祖先。
func checkFSBackupDir(deps Deps, opts Options) Check {
	if deps.Containerized {
		return skip(nameFSBackupDir, "容器路径请在宿主运行 selfcheck（备份目录在宿主）")
	}
	dir := opts.BackupDir
	if dir == "" {
		return fail(nameFSBackupDir, "备份目录为空",
			"用 --backup-dir 指定目录（缺省 "+DefaultBackupDir+"）")
	}
	info, err := deps.FS.Stat(dir)
	if err == nil {
		if !info.IsDir() {
			return fail(nameFSBackupDir, "不是目录："+dir,
				"换一个目录或删除同名文件")
		}
		verdict := currentUserAccess(deps, dir, fsperm.Write|fsperm.Execute, "写")
		if verdict.skip != "" {
			return skip(nameFSBackupDir, verdict.skip)
		}
		if !verdict.allowed {
			return fail(nameFSBackupDir, verdict.detail,
				"更新器以 root 写备份：chmod 700 "+dir+" 并确保 root 可写（目录属主应为 root）")
		}
		return pass(nameFSBackupDir, verdict.detail)
	}
	if !errors.Is(err, os.ErrNotExist) {
		return fail(nameFSBackupDir, "无法读取目录 "+dir+"："+err.Error(),
			"检查路径与进程权限")
	}
	return missingBackupDirRow(deps, opts, dir)
}

// missingBackupDirRow 沿父目录上溯，按调用者（root）判最近祖先的可写性。
// 找不到可写祖先即 FAIL：更新在停机后才发现备份不了，代价更大。
func missingBackupDirRow(deps Deps, opts Options, dir string) Check {
	ancestor := parentDir(dir)
	for {
		info, err := deps.FS.Stat(ancestor)
		switch {
		case err == nil:
			if !info.IsDir() {
				return fail(nameFSBackupDir, "不存在，且最近路径 "+ancestor+" 不是目录",
					"换一个备份目录")
			}
			verdict := currentUserAccess(deps, ancestor, fsperm.Write|fsperm.Execute, "写")
			switch {
			case verdict.skip != "":
				return skip(nameFSBackupDir, verdict.skip+"（"+dir+" 不存在）")
			case !verdict.allowed:
				return fail(nameFSBackupDir,
					dir+" 不存在；最近的已存在目录 "+ancestor+" 上"+verdict.detail,
					"用 --backup-dir 指向可写目录，或先创建并 chown root:root")
			default:
				return skip(nameFSBackupDir,
					"不存在；最近的已存在目录 "+ancestor+" 上"+verdict.detail+"（"+dir+" 未创建）")
			}
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

// accessVerdict 是服务用户目录权限的判定结果。
type accessVerdict struct {
	allowed bool
	detail  string
	skip    string
}

// currentUserAccess 按**调用者**（通常是 root）判目录权限，用于备份目录。
// 身份与属主都按进程真实 uid/gid；探针缺失时 SKIP 并说明（不冒充 root）。
func currentUserAccess(deps Deps, path string, want fsperm.Access, verb string) accessVerdict {
	if deps.CurrentIdentity == nil {
		return accessVerdict{skip: "当前用户身份探针未注入（备份目录按调用者判权）"}
	}
	id, ok := deps.CurrentIdentity()
	if !ok {
		return accessVerdict{skip: "本平台拿不到当前用户身份（Windows），无法判备份目录权限"}
	}
	who := "当前用户"
	if id.Name != "" {
		who = "当前用户 " + id.Name
	}
	return judgeDirAccessFor(deps, path, id, who, want, verb)
}

// judgeDirAccess 按服务用户的 POSIX 属主/组/模式位判目录权限。
// 不读目录、不建临时文件；判据与 doctor 共用 core/fsperm。
func judgeDirAccess(deps Deps, opts Options, path string, want fsperm.Access, verb string) accessVerdict {
	if deps.StatPermission == nil || deps.UserIdentity == nil {
		return accessVerdict{skip: "权限探针未注入"}
	}
	owner, ok := deps.StatPermission(path)
	if !ok {
		return accessVerdict{skip: "本平台拿不到 POSIX 属主/权限位（Windows），无法按服务用户判权"}
	}
	if opts.ServiceUser == "" {
		return accessVerdict{skip: "服务用户未知（用 --service-user 指定；systemctl show 取不到）"}
	}
	id, ok := deps.UserIdentity(opts.ServiceUser)
	if !ok {
		return accessVerdict{skip: "查不到服务用户 " + opts.ServiceUser + " 的身份"}
	}
	return verdictForOwner(owner, id, "服务用户 "+opts.ServiceUser, want, verb)
}

// judgeDirAccessFor 用给定身份判目录权限；who 是报告里的身份描述。
func judgeDirAccessFor(deps Deps, path string, id fsperm.Identity, who string, want fsperm.Access, verb string) accessVerdict {
	if deps.StatPermission == nil {
		return accessVerdict{skip: "权限探针未注入"}
	}
	owner, ok := deps.StatPermission(path)
	if !ok {
		return accessVerdict{skip: "本平台拿不到 POSIX 属主/权限位（Windows），无法按" + who + "判权"}
	}
	return verdictForOwner(owner, id, who, want, verb)
}

// verdictForOwner 把「属主/模式 + 身份」折成 allowed/detail。
// root（uid 0）有 CAP_DAC_OVERRIDE：位只用来识别「明显不可写」的目录。
func verdictForOwner(owner fsperm.Owner, id fsperm.Identity, who string, want fsperm.Access, verb string) accessVerdict {
	allowed := fsperm.Allowed(owner, id, want)
	if id.HasIDs && id.UID == 0 {
		allowed = fsperm.RootAllows(owner, want)
	}
	if !allowed {
		return accessVerdict{detail: fmt.Sprintf("%s 无%s权限（属主 %s、组 %s、权限 %04o）",
			who, verb, ownerLabel(owner), groupLabel(owner), owner.Mode.Perm())}
	}
	return accessVerdict{allowed: true, detail: fmt.Sprintf("%s 可%s（属主 %s、组 %s、权限 %04o）",
		who, verb, ownerLabel(owner), groupLabel(owner), owner.Mode.Perm())}
}

// ownerLabel 优先名字，缺名字回退 uid。
func ownerLabel(owner fsperm.Owner) string {
	if owner.OwnerName != "" {
		return owner.OwnerName
	}
	if owner.HasIDs {
		return strconv.FormatUint(uint64(owner.UID), 10)
	}
	return "unknown"
}

// groupLabel 优先组名，缺名字回退 gid。
func groupLabel(owner fsperm.Owner) string {
	if owner.GroupName != "" {
		return owner.GroupName
	}
	if owner.HasIDs {
		return strconv.FormatUint(uint64(owner.GID), 10)
	}
	return "unknown"
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
