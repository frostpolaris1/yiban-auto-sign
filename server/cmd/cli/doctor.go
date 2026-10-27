// yiban-cli doctor：故障模式表驱动体检。
//
// # 功能
// 每条检查 = 检测 + 修复命令。全部外供依赖收敛在 probe：
// 环境来源、数据库探针、磁盘水位、时钟、端口探针都可注入，
// 测试据此注入故障环境（派单 P7 判据）。
// 本命令**不**修任何东西、**不**改库、**不**写文件（photo 目录的
// 可写探针用临时文件，用毕即删）。
//
// # 归属
// cmd/cli 子命令。检查表只在本文件；SQL 细节住在 probe.go 的 pgHandle，
// 平台差异（statfs、文件属主、执行位）在 doctor_platform_*.go。
//
// # 口径
// 退出码：有 FAIL 即 1，否则 0。SKIP 不是绿：跳过行必须带原因，
// 且数据库面检查在 db.dsn 未过时整体 SKIP 并点名前置。
package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"sort"
	"strings"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
)

// diskFloorBytes 是磁盘水位的下限：photo 目录所在卷剩余低于它即 FAIL。
const diskFloorBytes = uint64(1) << 30 // 1 GiB

// clockSkewLimit 是进程时钟与数据库时钟的容差。
const clockSkewLimit = 2 * time.Second

// checkRow 是一条检查的结果。
type checkRow struct {
	name   string
	title  string
	fail   bool
	skip   bool
	detail string // 事实与数字；SKIP 时是跳过原因
	fix    string // 修复命令；FAIL 时必填
}

// report 是一次体检的汇总。
type report struct {
	rows   []checkRow
	failed int
}

// text 渲染人读报告。FAIL 行必须带「修复:」命令（派单判据）。
func (r report) text() string {
	var b strings.Builder
	for _, row := range r.rows {
		switch {
		case row.fail:
			fmt.Fprintf(&b, "[FAIL] %-18s %s：%s\n       修复: %s\n", row.name, row.title, row.detail, row.fix)
		case row.skip:
			fmt.Fprintf(&b, "[SKIP] %-18s %s：%s\n", row.name, row.title, row.detail)
		default:
			fmt.Fprintf(&b, "[ OK ] %-18s %s：%s\n", row.name, row.title, row.detail)
		}
	}
	fmt.Fprintf(&b, "汇总：%d 过 / %d 败 / %d 跳\n",
		len(r.rows)-r.failed-countSkips(r.rows), r.failed, countSkips(r.rows))
	return b.String()
}

func countSkips(rows []checkRow) int {
	n := 0
	for _, r := range rows {
		if r.skip {
			n++
		}
	}
	return n
}

// probe 收敛体检的全部外供依赖。生产用 newProbe 构造，测试逐字段注入。
type probe struct {
	src config.Source
	// dsn 与 photoDir 是命令行覆盖；空则回退环境。
	dsn      string
	photoDir string
	registry string // 配置名册路径（见 registryCandidates）
	binPath  string // 自身二进制路径

	// serviceUser 是 systemd 单元里的运行用户（User=）。
	// 属主与可写性都按它判，不按当前进程用户判。
	serviceUser string
	// containerized 表示本进程跑在容器里：distroless 无 shell 与 chown，
	// 修复提示必须在宿主机执行。
	containerized bool

	now         func() time.Time
	tryListen   func(addr string) (closer, error)
	httpGet     func(addr, path string) (int, error)
	openDB      func(ctx context.Context, dsn string, pepper []byte) (dbHandle, error)
	diskFree    func(dir string) (uint64, error)
	currentUser func() (string, bool)
	// dirStat 返回目录的属主、组与权限位；拿不到返回 false。
	dirStat func(dir string) (dirInfo, bool)
	// userGroups 返回用户所属组名；拿不到返回 false。
	userGroups func(name string) ([]string, bool)
}

// dirInfo 是目录的身份与权限位。
type dirInfo struct {
	owner string
	group string
	perm  os.FileMode
}

// defaultServiceUser 是 systemd 单元里的服务用户。
// install.sh 的单元写死 User=yiban，两者同源。
const defaultServiceUser = "yiban"

// systemdUnitPath 是 install.sh 写的单元路径。
// 它存在 ⇒ 服务以 User=yiban 运行；不存在 ⇒ 本进程就是服务身份
// （容器或前台运行），缺省按当前用户判。
const systemdUnitPath = "/etc/systemd/system/yiban.service"

// unitFileExists 是单元存在性判定；测试替换它构造两种部署环境。
var unitFileExists = func(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

// currentUserLookup 是当前用户读取的接缝；测试替换它避免依赖真实账户。
var currentUserLookup = currentUserName

// resolveServiceUser 是缺省服务用户的纯判据（派单 P1-1）。
// 有 systemd 单元按 yiban；无单元按当前用户；拿不到当前用户回退 yiban。
func resolveServiceUser(exists func(string) bool, current func() (string, bool)) string {
	if exists(systemdUnitPath) {
		return defaultServiceUser
	}
	if name, ok := current(); ok && strings.TrimSpace(name) != "" {
		return name
	}
	return defaultServiceUser
}

// defaultServiceUserResolved 解析本机的缺省服务用户。
func defaultServiceUserResolved() string {
	return resolveServiceUser(unitFileExists, currentUserLookup)
}

// containerMarkers 是容器运行时留下的标记文件（Docker、Podman）。
var containerMarkers = []string{"/.dockerenv", "/run/.containerenv"}

// inContainer 判本进程是否跑在容器里。判据只看标记文件，不看 systemd：
// 没有 systemd 的裸机仍可在本机执行 chown。
func inContainer() bool {
	for _, path := range containerMarkers {
		if _, err := os.Stat(path); err == nil {
			return true
		}
	}
	return false
}

// registryCandidates 是名册的缺省查找顺序：安装位优先。
// install.sh 把 config/registry.json 装到 /etc/yiban/registry.json。
func registryCandidates() []string {
	return []string{"/etc/yiban/registry.json", "config/registry.json"}
}

// pickRegistry 解析名册路径：显式值优先，否则取首个存在的候选。
// 全部缺失时返回末位候选：SKIP 文案点名它，路径不出自空气。
func pickRegistry(explicit string, exists func(string) bool, candidates ...string) string {
	if strings.TrimSpace(explicit) != "" {
		return explicit
	}
	last := ""
	for _, c := range candidates {
		last = c
		if exists(c) {
			return c
		}
	}
	return last
}

// serviceUserOf 返回服务用户名：探针给定值优先，否则缺省。
func serviceUserOf(p *probe) string {
	if v := strings.TrimSpace(p.serviceUser); v != "" {
		return v
	}
	return defaultServiceUser
}

// writableByServiceUser 按属主、组、其他三类写位判断服务用户是否可写。
// 判据不落盘、不借用当前进程身份：doctor 常以 root 跑，
// root 自己可写不代表服务用户可写。
func writableByServiceUser(info dirInfo, user string, p *probe) bool {
	if info.perm&0o002 != 0 {
		return true
	}
	if info.owner == user {
		return info.perm&0o200 != 0
	}
	if info.perm&0o020 == 0 {
		return false
	}
	groups, ok := p.userGroups(user)
	if !ok {
		return false
	}
	for _, g := range groups {
		if g == info.group {
			return true
		}
	}
	return false
}

// closer 是端口探针拿到的监听句柄；探完即关。
type closer interface{ Close() error }

// dbHandle 是体检用的数据库语义面。SQL 只住在 pgHandle。
type dbHandle interface {
	Close()
	SchemaVersion(ctx context.Context) (int64, error)
	ServerVersionNum(ctx context.Context) (int64, error)
	TableExists(ctx context.Context, name string) (bool, error)
	DBNow(ctx context.Context) (time.Time, error)
	// Count 数表行数；where 取本文件登记的固定形状，不拼用户输入。
	Count(ctx context.Context, table, where string, args ...any) (int64, error)
	AuditVerify(ctx context.Context) (coreaudit.Report, error)
}

// expectedTables 是 schema 必须存在的表（迁移 00001–00016 的全集）。
var expectedTables = []string{
	"users", "sessions", "schools", "admin_schools", "accounts", "credentials",
	"audit_logs", "password_resets", "school_tasks", "window_cache", "sign_tasks",
	"egresses", "account_egress", "time_prefs", "window_overrides", "photos",
	"photo_retention", "account_location",
}

// expectedTableSet 返回表全集的集合形状，测试共用同一份名册。
func expectedTableSet() map[string]bool {
	m := make(map[string]bool, len(expectedTables))
	for _, t := range expectedTables {
		m[t] = true
	}
	return m
}

// check 是单条故障模式。
type check struct {
	name  string
	title string
	run   func(ctx context.Context, p *probe) checkRow
}

// checks 是故障模式清单（派单 P7 首批 ≥20 条）。
// 新增检查在此登记；每条必须能在 doctor_test.go 里注入故障。
var checks = []check{
	{"env.config", "环境变量通过配置校验", checkEnvConfig},
	{"env.pepper", "YIBAN_HASH_PEPPER 强度", checkPepper},
	{"env.credential_key", "YIBAN_CREDENTIAL_KEY 形状", checkCredentialKey},
	{"env.tz_set", "YIBAN_TZ 显式设置", checkTZSet},
	{"env.tz_name", "YIBAN_TZ 是合法时区名", checkTZName},
	{"env.unknown_keys", "YIBAN_* 键全部登记在名册", checkUnknownKeys},
	{"db.dsn", "数据库可连接", checkDSN},
	{"db.schema_version", "schema 版本与二进制一致", checkSchemaVersion},
	{"db.tables", "schema 表齐全", checkTables},
	{"db.pg_version", "PostgreSQL 版本不低于 18", checkPGVersion},
	{"db.clock_offset", "数据库时钟与进程时钟同步", checkClockOffset},
	{"db.audit_chain", "审计链完整可校验", checkAuditChain},
	{"db.pepper_rotation", "审计链断裂时的轮换提示", checkPepperRotation},
	{"db.egress", "出口表非空", checkEgress},
	{"db.school_tasks", "存在启用的签到任务", checkSchoolTasks},
	{"db.no_running", "无 running 僵尸行", checkNoRunning},
	{"fs.photo_dir", "photo 目录可写", checkPhotoDir},
	{"fs.disk", "photo 目录所在盘水位", checkDisk},
	{"fs.photo_owner", "photo 目录属主与服务用户一致", checkPhotoOwner},
	{"net.http_addr", "HTTP 端口可用", checkHTTPAddr},
	{"bin.executable", "自身二进制可执行", checkBinExecutable},
}

// runChecks 顺序跑全部检查并汇总。
func runChecks(ctx context.Context, p *probe) report {
	rep := report{}
	for _, c := range checks {
		row := c.run(ctx, p)
		row.name = c.name
		row.title = c.title
		if row.fail {
			rep.failed++
		}
		rep.rows = append(rep.rows, row)
	}
	return rep
}

// lookupStr 读一个字符串键并去空白。
func lookupStr(src config.Source, key string) (string, bool) {
	v, ok := src.Lookup(key)
	return strings.TrimSpace(v), ok
}

// photoDirOf 解析 photo 目录：命令行覆盖 > 环境 > 缺省。
func photoDirOf(p *probe) string {
	if p.photoDir != "" {
		return p.photoDir
	}
	if v, ok := lookupStr(p.src, "YIBAN_PHOTO_DIR"); ok && v != "" {
		return v
	}
	return config.DefaultPhotoDir
}

// checkEnvConfig 整体过一遍配置校验，错误映射到对应修复命令。
func checkEnvConfig(_ context.Context, p *probe) checkRow {
	_, err := config.LoadFrom(p.src)
	if err == nil {
		return checkRow{detail: "配置可加载"}
	}
	msg := err.Error()
	switch {
	case strings.Contains(msg, "YIBAN_HASH_PEPPER"):
		return checkRow{fail: true, detail: msg,
			fix: `openssl rand -hex 32 生成新值写入 YIBAN_HASH_PEPPER（轮换会让审计链失效，先看 db.pepper_rotation）`}
	case strings.Contains(msg, "YIBAN_CREDENTIAL_KEY"):
		return checkRow{fail: true, detail: msg,
			fix: `openssl rand -base64 32 生成新值写入 YIBAN_CREDENTIAL_KEY`}
	case strings.Contains(msg, "YIBAN_ENV"):
		return checkRow{fail: true, detail: msg,
			fix: `设置 YIBAN_ENV=production（可选 development/test/staging）`}
	case strings.Contains(msg, "YIBAN_INSECURE_COOKIES"):
		return checkRow{fail: true, detail: msg,
			fix: `删除 YIBAN_INSECURE_COOKIES 或设为 false（production 拒绝不安全 cookie）`}
	default:
		return checkRow{fail: true, detail: msg,
			fix: `按 config/registry.json 的类型与域修正对应 YIBAN_* 键`}
	}
}

// checkPepper 独立检测 pepper 强度（不等配置校验整体失败）。
func checkPepper(_ context.Context, p *probe) checkRow {
	raw, ok := lookupStr(p.src, "YIBAN_HASH_PEPPER")
	if !ok || raw == "" {
		return checkRow{fail: true, detail: "YIBAN_HASH_PEPPER 未设置",
			fix: `openssl rand -hex 32 生成后写入 /etc/yiban/.env 的 YIBAN_HASH_PEPPER`}
	}
	if len(raw) < 16 {
		return checkRow{fail: true, detail: fmt.Sprintf("YIBAN_HASH_PEPPER 只有 %d 字符，至少 16", len(raw)),
			fix: `openssl rand -hex 32 生成后写入 YIBAN_HASH_PEPPER`}
	}
	return checkRow{detail: fmt.Sprintf("长度 %d 字符", len(raw))}
}

// checkCredentialKey 独立检测凭据钥形状：base64 且解码后 32 字节。
func checkCredentialKey(_ context.Context, p *probe) checkRow {
	raw, ok := lookupStr(p.src, "YIBAN_CREDENTIAL_KEY")
	if !ok || raw == "" {
		return checkRow{fail: true, detail: "YIBAN_CREDENTIAL_KEY 未设置",
			fix: `openssl rand -base64 32 生成后写入 /etc/yiban/.env 的 YIBAN_CREDENTIAL_KEY`}
	}
	key, err := base64StdDecode(raw)
	if err != nil {
		return checkRow{fail: true, detail: "YIBAN_CREDENTIAL_KEY 不是合法 base64",
			fix: `openssl rand -base64 32 生成后写入 YIBAN_CREDENTIAL_KEY`}
	}
	if len(key) != 32 {
		return checkRow{fail: true, detail: fmt.Sprintf("YIBAN_CREDENTIAL_KEY 解码后 %d 字节，需要 32", len(key)),
			fix: `openssl rand -base64 32 生成后写入 YIBAN_CREDENTIAL_KEY`}
	}
	return checkRow{detail: "32 字节 base64 形状正确"}
}

// checkTZSet 要求显式设置时区：业务日边界依赖它，静默缺省是配置味。
func checkTZSet(_ context.Context, p *probe) checkRow {
	raw, ok := lookupStr(p.src, "YIBAN_TZ")
	if !ok || raw == "" {
		return checkRow{fail: true, detail: "YIBAN_TZ 未设置（当前会静默用缺省 " + config.DefaultTimezone + "）",
			fix: `在 /etc/yiban/.env 写 YIBAN_TZ=<IANA 时区名>，例如 YIBAN_TZ=UTC`}
	}
	return checkRow{detail: "已显式设置 " + raw}
}

// checkTZName 校验时区名能加载。
func checkTZName(_ context.Context, p *probe) checkRow {
	name, ok := lookupStr(p.src, "YIBAN_TZ")
	if !ok || name == "" {
		name = config.DefaultTimezone
	}
	if _, err := coreclock.LoadLocation(name); err != nil {
		return checkRow{fail: true, detail: fmt.Sprintf("时区 %q 无法加载: %v", name, err),
			fix: `设置 YIBAN_TZ=<IANA 时区名>；可用 tzselect 查名`}
	}
	return checkRow{detail: "时区 " + name + " 可加载"}
}

// checkUnknownKeys 枚举环境里的 YIBAN_* 键，逐个对名册。
// 名册缺失显性跳过：扫空不得当绿。
func checkUnknownKeys(_ context.Context, p *probe) checkRow {
	es, ok := p.src.(config.EnumeratedSource)
	if !ok {
		return checkRow{skip: true, detail: "跳过：环境来源不可枚举（测试替身未实现 Environ）"}
	}
	body, err := os.ReadFile(p.registry)
	if err != nil {
		return checkRow{skip: true, detail: fmt.Sprintf("跳过：名册文件不可读 %s（--registry 指定路径）", p.registry)}
	}
	var reg map[string]json.RawMessage
	if err := json.Unmarshal(body, &reg); err != nil {
		return checkRow{skip: true, detail: fmt.Sprintf("跳过：名册 JSON 解析失败 %s: %v", p.registry, err)}
	}
	unknown := []string{}
	for _, key := range config.PrefixedKeys(es, "YIBAN_") {
		if _, in := reg[key]; !in {
			unknown = append(unknown, key)
		}
	}
	if len(unknown) > 0 {
		sort.Strings(unknown)
		return checkRow{fail: true, detail: "未登记键：" + strings.Join(unknown, ", "),
			fix: "把上述键登记进 config/registry.json，或从环境删除"}
	}
	return checkRow{detail: "环境里的 YIBAN_* 键全部登记"}
}

// openDBOrSkip 打开数据库探针；失败返回 SKIP 行并点名前置 db.dsn。
// 数据库面的每条检查独立开连：体检是低频运维动作，
// 换来的是每条检查独立可测、无共享状态。
func openDBOrSkip(ctx context.Context, p *probe) (dbHandle, checkRow) {
	h, failRow := openDBHandle(ctx, p)
	if h == nil {
		return nil, checkRow{skip: true,
			detail: "跳过：前置 db.dsn 未通过（" + failRow.detail + "）"}
	}
	return h, checkRow{}
}

// openDBHandle 解析 DSN 并打开探针。失败返回带修复命令的 FAIL 行。
func openDBHandle(ctx context.Context, p *probe) (dbHandle, checkRow) {
	dsn := p.dsn
	if dsn == "" {
		v, _ := lookupStr(p.src, "YIBAN_DATABASE_URL")
		dsn = v
	}
	if dsn == "" {
		return nil, checkRow{fail: true, detail: "YIBAN_DATABASE_URL 未设置",
			fix: `设置 YIBAN_DATABASE_URL=postgres://用户:口令@主机:5432/库?sslmode=disable`}
	}
	pepper, _ := lookupStr(p.src, "YIBAN_HASH_PEPPER")
	h, err := p.openDB(ctx, dsn, []byte(pepper))
	if err != nil {
		return nil, checkRow{fail: true, detail: fmt.Sprintf("DSN %s 不可达: %v", redactDSN(dsn), err),
			fix: `psql "你的DSN" -c 'select 1' 先行核对主机/端口/口令/库名`}
	}
	return h, checkRow{}
}

// redactDSN 抹掉 DSN 里的口令段再打印（派单 F6、P1-2）。
// 三种载体都要覆盖：URI、keyword/value、无 scheme 的 user:pass@host。
// keyword 形按 libpq 词法切分：= 两侧可带空格，值可用单引号包住空白。
// URI 的查询串另抹 password 参数（?password=... 同样不能打印）。
// 只抹口令，主机与库名保留。
func redactDSN(dsn string) string {
	out := redactURIDS(dsn)
	if strings.Contains(out, "://") {
		return redactURIPasswordQuery(out)
	}
	if masked, ok := redactKeywordDSN(out); ok {
		return masked
	}
	return out
}

// redactURIPasswordQuery 抹掉 URI 查询串里的 password 参数。
// 逐参数切分，只替换口令值；其他参数原样保留。
func redactURIPasswordQuery(dsn string) string {
	q := strings.Index(dsn, "?")
	if q < 0 {
		return dsn
	}
	parts := strings.Split(dsn[q+1:], "&")
	for i, part := range parts {
		key, _, ok := strings.Cut(part, "=")
		if ok && strings.EqualFold(strings.TrimSpace(key), "password") {
			parts[i] = key + "=****"
		}
	}
	return dsn[:q+1] + strings.Join(parts, "&")
}

// redactKeywordDSN 逐字段重建 keyword/value DSN，password 的整值换成 ****。
// 带引号时连引号一起替换：只抹到首个空格会留下尾部片段。
// ok=false 表示不是键值形（无 = 的裸 user:pass@host）。
func redactKeywordDSN(dsn string) (string, bool) {
	fields := splitKeywordFields(dsn)
	if len(fields) == 0 {
		return dsn, false
	}
	out := make([]string, 0, len(fields))
	for _, field := range fields {
		key, _, ok := strings.Cut(field, "=")
		if !ok || strings.TrimSpace(key) == "" {
			return dsn, false
		}
		if strings.EqualFold(strings.TrimSpace(key), "password") {
			out = append(out, key+"=****")
			continue
		}
		out = append(out, field)
	}
	return strings.Join(out, " "), true
}

// redactURIDS 处理 `<scheme>://user:pass@host` 与 `user:pass@host`。
// 只在首个空白分隔的 token 内找 userinfo；token 含 `=` 即 keyword 形，
// 本函数不动它，避免把主机段当口令段切坏。
func redactURIDS(dsn string) string {
	start := 0
	if i := strings.Index(dsn, "://"); i >= 0 {
		start = i + 3
	}
	rest := dsn[start:]
	tokenEnd := len(rest)
	for i := 0; i < len(rest); i++ {
		if rest[i] == ' ' || rest[i] == '\t' {
			tokenEnd = i
			break
		}
	}
	at := strings.Index(rest[:tokenEnd], "@")
	if at < 0 {
		return dsn
	}
	userinfo := rest[:at]
	if strings.Contains(userinfo, "=") {
		return dsn
	}
	colon := strings.Index(userinfo, ":")
	if colon < 0 {
		return dsn
	}
	return dsn[:start] + userinfo[:colon] + ":****@" + rest[at+1:]
}

// checkDSN 是数据库面的第 0 条：其余 db.* 检查以它为前置。
func checkDSN(ctx context.Context, p *probe) checkRow {
	h, row := openDBHandle(ctx, p)
	if h == nil {
		return row
	}
	h.Close()
	return checkRow{detail: "连接成功"}
}

func checkSchemaVersion(ctx context.Context, p *probe) checkRow {
	h, skip := openDBOrSkip(ctx, p)
	if h == nil {
		return skip
	}
	defer h.Close()
	embedded, err := pg.EmbeddedVersion()
	if err != nil {
		return checkRow{skip: true, detail: "跳过：读取嵌入版本失败: " + err.Error()}
	}
	current, err := h.SchemaVersion(ctx)
	if err != nil {
		return checkRow{skip: true, detail: "跳过：读取 schema 版本失败: " + err.Error()}
	}
	switch {
	case current < embedded:
		return checkRow{fail: true, detail: fmt.Sprintf("数据库版本 %d 落后于二进制 %d", current, embedded),
			fix: "yiban-cli migrate"}
	case current > embedded:
		return checkRow{fail: true, detail: fmt.Sprintf("数据库版本 %d 超前于二进制 %d（降级被禁止）", current, embedded),
			fix: "换用与数据库 schema 匹配的新版二进制后重试"}
	}
	return checkRow{detail: fmt.Sprintf("版本一致（%d）", current)}
}

func checkTables(ctx context.Context, p *probe) checkRow {
	h, skip := openDBOrSkip(ctx, p)
	if h == nil {
		return skip
	}
	defer h.Close()
	missing := []string{}
	for _, table := range expectedTables {
		ok, err := h.TableExists(ctx, table)
		if err != nil {
			return checkRow{skip: true, detail: "跳过：判表失败: " + err.Error()}
		}
		if !ok {
			missing = append(missing, table)
		}
	}
	if len(missing) > 0 {
		return checkRow{fail: true, detail: "缺表：" + strings.Join(missing, ", "),
			fix: "yiban-cli migrate"}
	}
	return checkRow{detail: fmt.Sprintf("%d 张表齐全", len(expectedTables))}
}

func checkPGVersion(ctx context.Context, p *probe) checkRow {
	h, skip := openDBOrSkip(ctx, p)
	if h == nil {
		return skip
	}
	defer h.Close()
	num, err := h.ServerVersionNum(ctx)
	if err != nil {
		return checkRow{skip: true, detail: "跳过：读 server_version_num 失败: " + err.Error()}
	}
	if num < 180000 {
		return checkRow{fail: true, detail: fmt.Sprintf("PostgreSQL server_version_num=%d，低于 18", num),
			fix: `升级 PostgreSQL 至 18（pg_dump/pg_restore 迁移数据）`}
	}
	return checkRow{detail: fmt.Sprintf("server_version_num=%d", num)}
}

func checkClockOffset(ctx context.Context, p *probe) checkRow {
	h, skip := openDBOrSkip(ctx, p)
	if h == nil {
		return skip
	}
	defer h.Close()
	dbNow, err := h.DBNow(ctx)
	if err != nil {
		return checkRow{skip: true, detail: "跳过：读数据库时钟失败: " + err.Error()}
	}
	offset := p.now().Sub(dbNow)
	if offset < 0 {
		offset = -offset
	}
	if offset > clockSkewLimit {
		return checkRow{fail: true, detail: fmt.Sprintf("数据库与进程时钟差 %s，超过 %s", offset, clockSkewLimit),
			fix: `校时：timedatectl set-ntp true 或 chronyc tracking 核对 NTP 状态`}
	}
	return checkRow{detail: fmt.Sprintf("偏移 %s（容差 %s）", offset, clockSkewLimit)}
}

func checkAuditChain(ctx context.Context, p *probe) checkRow {
	h, skip := openDBOrSkip(ctx, p)
	if h == nil {
		return skip
	}
	defer h.Close()
	rep, err := h.AuditVerify(ctx)
	return auditChainRow(rep, err, false)
}

// checkPepperRotation 与 db.audit_chain 共用检测、分开给修复：
// 断链最常见的原因是 pepper 被轮换；恢复原值即可，不必当事件处理。
func checkPepperRotation(ctx context.Context, p *probe) checkRow {
	h, skip := openDBOrSkip(ctx, p)
	if h == nil {
		return skip
	}
	defer h.Close()
	rep, err := h.AuditVerify(ctx)
	return auditChainRow(rep, err, true)
}

// auditChainRow 把链校验结果渲染成检查行；rotation=true 换修复口径。
func auditChainRow(rep coreaudit.Report, err error, rotation bool) checkRow {
	if errors.Is(err, coreaudit.ErrEmptyChain) {
		return checkRow{skip: true, detail: "跳过：审计链为空（新部署尚无操作）"}
	}
	if errors.Is(err, coreaudit.ErrBrokenChain) {
		if rotation {
			return checkRow{fail: true, detail: "审计链断裂，近期轮换过 YIBAN_HASH_PEPPER 是最常见原因",
				fix: `恢复 /etc/yiban/.env 里原 YIBAN_HASH_PEPPER 后重启；确认未轮换再按 db.audit_chain 处理`}
		}
		return checkRow{fail: true, detail: fmt.Sprintf("审计链在校验区间 [%d,%d] 断裂", rep.FirstID, rep.LastID),
			fix: `保留现场并排查：先看 db.pepper_rotation；确认无轮换后按安全事件处理（链被改写）`}
	}
	if err != nil {
		return checkRow{skip: true, detail: "跳过：审计链校验失败: " + err.Error()}
	}
	detail := fmt.Sprintf("全链通过（%d 行）", rep.Count)
	if rotation {
		detail = "链完整，无轮换迹象"
	}
	return checkRow{detail: detail}
}

func checkEgress(ctx context.Context, p *probe) checkRow {
	h, skip := openDBOrSkip(ctx, p)
	if h == nil {
		return skip
	}
	defer h.Close()
	n, err := h.Count(ctx, "egresses", "")
	if err != nil {
		return checkRow{skip: true, detail: "跳过：数出口失败: " + err.Error()}
	}
	if n == 0 {
		return checkRow{fail: true, detail: "egresses 表为空：执行循环无出口可分配，任务全部等待",
			fix: `INSERT INTO egresses (identity) VALUES ('http://你的出口:端口');（或在管理端录入）`}
	}
	return checkRow{detail: fmt.Sprintf("%d 条出口", n)}
}

func checkSchoolTasks(ctx context.Context, p *probe) checkRow {
	h, skip := openDBOrSkip(ctx, p)
	if h == nil {
		return skip
	}
	defer h.Close()
	n, err := h.Count(ctx, "school_tasks", "enabled")
	if err != nil {
		return checkRow{skip: true, detail: "跳过：数任务失败: " + err.Error()}
	}
	if n == 0 {
		return checkRow{fail: true, detail: "没有启用的 school_tasks：调度面无事可做",
			fix: `在管理端启用至少一个签到任务（school_tasks.enabled = true）`}
	}
	return checkRow{detail: fmt.Sprintf("%d 个启用任务", n)}
}

func checkNoRunning(ctx context.Context, p *probe) checkRow {
	h, skip := openDBOrSkip(ctx, p)
	if h == nil {
		return skip
	}
	defer h.Close()
	n, err := h.Count(ctx, "sign_tasks", "state = $1", string(corestatus.TaskRunning))
	if err != nil {
		return checkRow{skip: true, detail: "跳过：数 running 失败: " + err.Error()}
	}
	if n > 0 {
		return checkRow{fail: true, detail: fmt.Sprintf("%d 行停在 running：上次进程未优雅收尾或正在执行", n),
			fix: `服务运行中属正常，请在停机后重跑本检查；停机后仍残留则重启服务（启动自动回收），仍残留再人工把 running 行重置回 pending`}
	}
	return checkRow{detail: "无 running 行"}
}

func checkPhotoDir(_ context.Context, p *probe) checkRow {
	dir := photoDirOf(p)
	svc := serviceUserOf(p)
	me, okMe := p.currentUser()
	if !okMe || me == svc {
		// 同一身份：直接实测写入，判据最硬。
		if err := probeWritable(dir); err != nil {
			return checkRow{fail: true, detail: fmt.Sprintf("photo 目录 %s 不可写: %v", dir, err),
				fix: photoDirFix(p, dir, svc)}
		}
		return checkRow{detail: fmt.Sprintf("%s 可写（服务用户 %s）", dir, svc)}
	}
	// 当前进程不是服务用户：按属主/组/权限位推断，
	// 不把自己的可写性当服务用户的可写性。
	info, ok := p.dirStat(dir)
	if !ok {
		return checkRow{skip: true, detail: fmt.Sprintf(
			"跳过：读不到 %s 的属主与权限位（或目录不存在）；当前用户 %s 与服务用户 %s 不同，实测写入无判别力", dir, me, svc)}
	}
	if !writableByServiceUser(info, svc, p) {
		return checkRow{fail: true, detail: fmt.Sprintf("%s 属主 %s、权限 %04o：服务用户 %s 不可写",
			dir, info.owner, info.perm.Perm(), svc),
			fix: photoOwnerFix(p, dir, svc)}
	}
	return checkRow{detail: fmt.Sprintf("属主 %s、权限 %04o：服务用户 %s 可写", info.owner, info.perm.Perm(), svc)}
}

// photoDirFix 是 photo 目录不可写时的修复提示。
func photoDirFix(p *probe, dir, svc string) string {
	if p.containerized {
		return containerPhotoFix(dir)
	}
	return fmt.Sprintf("mkdir -p %s && chown -R %s:%s %s", dir, svc, svc, dir)
}

// photoOwnerFix 是目录属主与服务用户不一致时的修复提示。
func photoOwnerFix(p *probe, dir, svc string) string {
	if p.containerized {
		return containerPhotoFix(dir)
	}
	return fmt.Sprintf("chown -R %s:%s %s（systemd 单元以 User=%s 运行）", svc, svc, dir, svc)
}

// containerPhotoFix 给宿主机可执行的修复。
// distroless 镜像没有 shell 与 chown：容器内跑 chown 必然失败。
// 结构性修法是让镜像预建目录并 chown（deploy/docker/Dockerfile）。
func containerPhotoFix(dir string) string {
	return fmt.Sprintf("在宿主机执行（distroless 镜像内无 shell/chown）："+
		"docker run --rm -u 0 -v <照片卷>:%s busybox chown -R 65532:65532 %s；"+
		"或让镜像预建并 chown %s（deploy/docker/Dockerfile 的 COPY --chown=65532:65532）",
		dir, dir, dir)
}

func checkDisk(_ context.Context, p *probe) checkRow {
	dir := photoDirOf(p)
	free, err := p.diskFree(dir)
	if err != nil {
		return checkRow{skip: true, detail: "跳过：读磁盘水位失败: " + err.Error()}
	}
	if free < diskFloorBytes {
		return checkRow{fail: true, detail: fmt.Sprintf("%s 所在卷剩余 %d MiB，低于 %d MiB", dir, free>>20, diskFloorBytes>>20),
			fix: fmt.Sprintf("清理 %s 所在卷：du -xsh %s/* | sort -h 定位大头", dir, dir)}
	}
	return checkRow{detail: fmt.Sprintf("剩余 %d MiB", free>>20)}
}

// checkPhotoOwner 比对目录属主与服务用户。
// 服务用户是 systemd 单元声明的运行用户（--service-user，缺省 yiban）；
// 不是 doctor 自己的进程用户。
func checkPhotoOwner(_ context.Context, p *probe) checkRow {
	dir := photoDirOf(p)
	svc := serviceUserOf(p)
	info, ok := p.dirStat(dir)
	if !ok {
		return checkRow{skip: true, detail: "跳过：本平台拿不到文件属主（Windows）或目录不存在"}
	}
	if info.owner != svc {
		return checkRow{fail: true, detail: fmt.Sprintf("%s 属主是 %s，服务用户是 %s", dir, info.owner, svc),
			fix: photoOwnerFix(p, dir, svc)}
	}
	return checkRow{detail: fmt.Sprintf("属主 %s 与服务用户一致", info.owner)}
}

func checkHTTPAddr(_ context.Context, p *probe) checkRow {
	addr, ok := lookupStr(p.src, "YIBAN_HTTP_ADDR")
	if !ok || addr == "" {
		addr = config.DefaultHTTPAddr
	}
	if code, err := p.httpGet(addr, "/healthz"); err == nil && code == http.StatusOK {
		return checkRow{detail: "服务在跑且 /healthz 应答 200"}
	}
	ln, err := p.tryListen(addr)
	if err != nil {
		return checkRow{fail: true, detail: fmt.Sprintf("%s 被占用且 /healthz 不通（占用者不是本服务）: %v", addr, err),
			fix: fmt.Sprintf("ss -ltnp | grep %s 查占用者；改 YIBAN_HTTP_ADDR 或停掉占用进程", portOf(addr))}
	}
	_ = ln.Close()
	return checkRow{detail: addr + " 空闲可绑定"}
}

func checkBinExecutable(_ context.Context, p *probe) checkRow {
	if p.binPath == "" {
		return checkRow{skip: true, detail: "跳过：二进制路径未知"}
	}
	exec, err := isExecutable(p.binPath)
	if err != nil {
		return checkRow{skip: true, detail: "跳过：无法读取二进制状态: " + err.Error()}
	}
	if !exec {
		return checkRow{fail: true, detail: p.binPath + " 无执行位",
			fix: "chmod +x " + p.binPath}
	}
	return checkRow{detail: p.binPath + " 可执行"}
}

// portOf 取 addr 的端口段；无端口返回原串。
func portOf(addr string) string {
	if i := strings.LastIndex(addr, ":"); i >= 0 {
		return addr[i+1:]
	}
	return addr
}

// probeWritable 在目录里写探针文件后立即删除。
// 目录不存在会先尝试创建：与 storage.NewFS 的行为一致。
func probeWritable(dir string) error {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	f, err := os.CreateTemp(dir, ".doctor-probe-*")
	if err != nil {
		return err
	}
	name := f.Name()
	_, werr := f.WriteString("probe")
	cerr := f.Close()
	rerr := os.Remove(name)
	switch {
	case werr != nil:
		return werr
	case cerr != nil:
		return cerr
	default:
		return rerr
	}
}

// base64StdDecode 是标准 base64 解码的本文件别名。
func base64StdDecode(s string) ([]byte, error) {
	return base64.StdEncoding.DecodeString(s)
}

// newProbe 装配生产探针：真环境、真网络、真 statfs。
func newProbe(dsn, photoDir, registry, binPath, serviceUser string) *probe {
	if binPath == "" {
		if self, err := os.Executable(); err == nil {
			binPath = self
		}
	}
	if registry == "" {
		registry = pickRegistry("", fileExists, registryCandidates()...)
	}
	return &probe{
		src:           config.Env{},
		dsn:           dsn,
		photoDir:      photoDir,
		registry:      registry,
		binPath:       binPath,
		serviceUser:   serviceUser,
		containerized: inContainer(),
		now:           func() time.Time { return coreclock.System{}.Now() },
		tryListen: func(addr string) (closer, error) {
			return net.Listen("tcp", addr)
		},
		httpGet: func(addr, path string) (int, error) {
			client := &http.Client{Timeout: 3 * time.Second}
			resp, err := client.Get("http://" + addr + path)
			if err != nil {
				return 0, err
			}
			defer resp.Body.Close()
			_, _ = io.Copy(io.Discard, resp.Body)
			return resp.StatusCode, nil
		},
		openDB:      openPgProbe,
		diskFree:    statfsFree,
		currentUser: currentUserName,
		dirStat:     dirStatOf,
		userGroups:  userGroupNames,
	}
}

// fileExists 是 pickRegistry 的存在判定。
func fileExists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

// runDoctor 是 doctor 子命令入口。返回进程退出码。
func runDoctor(args []string, stdout, stderr io.Writer) int {
	fs := newFlagSet("doctor", stderr)
	dsn := fs.String("dsn", "", "覆盖 YIBAN_DATABASE_URL")
	photoDir := fs.String("photo-dir", "", "覆盖 YIBAN_PHOTO_DIR")
	registry := fs.String("registry", "", "配置名册路径（缺省依次试 /etc/yiban/registry.json 与 config/registry.json）")
	binPath := fs.String("bin", "", "被检二进制路径（缺省为自身）")
	serviceUser := fs.String("service-user", defaultServiceUserResolved(),
		"systemd 单元的运行用户（User=）；无单元时取当前用户")
	fs.Usage = func() { doctorUsage(fs, stderr) }
	if err := fs.Parse(args); err != nil {
		return 2
	}
	p := newProbe(*dsn, *photoDir, *registry, *binPath, *serviceUser)
	rep := runChecks(context.Background(), p)
	fmt.Fprint(stdout, rep.text())
	if rep.failed > 0 {
		return 1
	}
	return 0
}

// doctorUsage 打印分节帮助：--help 判据要求检查清单按面分组。
func doctorUsage(fs *flag.FlagSet, w io.Writer) {
	fmt.Fprintf(w, `用法: yiban-cli doctor [选项]

故障模式体检。每条 FAIL 输出一条修复命令；SKIP 写明原因。
退出码：有 FAIL 即 1。

选项:
`)
	fs.SetOutput(w)
	fs.PrintDefaults()
	fmt.Fprintf(w, `
检查清单（%d 项，按面分组）:
  环境面: `, len(checks))
	for _, c := range checks {
		if strings.HasPrefix(c.name, "env.") {
			fmt.Fprintf(w, "%s ", c.name)
		}
	}
	fmt.Fprintf(w, "\n  数据库面: ")
	for _, c := range checks {
		if strings.HasPrefix(c.name, "db.") {
			fmt.Fprintf(w, "%s ", c.name)
		}
	}
	fmt.Fprintf(w, "\n  文件系统面: ")
	for _, c := range checks {
		if strings.HasPrefix(c.name, "fs.") {
			fmt.Fprintf(w, "%s ", c.name)
		}
	}
	fmt.Fprintf(w, "\n  网络与运行件面: ")
	for _, c := range checks {
		if strings.HasPrefix(c.name, "net.") || strings.HasPrefix(c.name, "bin.") {
			fmt.Fprintf(w, "%s ", c.name)
		}
	}
	fmt.Fprintln(w)
}
