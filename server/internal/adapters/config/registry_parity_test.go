package config

import (
	"encoding/json"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"testing"
)

// registryEntry 是本测试从名册条目里需要的两个字段。
// Default 保留原始 JSON：类型由 keySpec.fallback 决定，禁止先猜类型。
// Domain 同理：数值域与 enum 的字符串域形状不同，按 keySpec 分派解析。
type registryEntry struct {
	Default json.RawMessage `json:"default"`
	Domain  json.RawMessage `json:"domain"`
}

// hasDefault 报告名册条目是否登记了缺省值（null 与缺字段同义）。
func (e registryEntry) hasDefault() bool {
	return len(e.Default) > 0 && string(e.Default) != "null"
}

// hasDomain 报告名册条目是否登记了域（null 与缺字段同义）。
func (e registryEntry) hasDomain() bool {
	return len(e.Domain) > 0 && string(e.Domain) != "null"
}

// loadRegistry 读回嵌入名册的原始条目。
// 逐条解析而不是整表解析：YIBAN_ENV 的域是字符串闭集，整表按数值域解会失败。
func loadRegistry(t *testing.T) map[string]json.RawMessage {
	t.Helper()
	body := RegistryJSON()
	if len(body) == 0 {
		t.Fatal("嵌入名册为空")
	}
	var reg map[string]json.RawMessage
	if err := json.Unmarshal(body, &reg); err != nil {
		t.Fatalf("解析名册失败: %v", err)
	}
	if len(reg) < 2 {
		t.Fatalf("名册只有 %d 条（含 _meta）——扫空不得当绿", len(reg))
	}
	return reg
}

// entryOf 解析单条名册条目的缺省与数值域。
func entryOf(t *testing.T, reg map[string]json.RawMessage, key string) (registryEntry, bool) {
	t.Helper()
	raw, ok := reg[key]
	if !ok {
		return registryEntry{}, false
	}
	var entry registryEntry
	if err := json.Unmarshal(raw, &entry); err != nil {
		t.Fatalf("解析名册条目 %s 失败: %v", key, err)
	}
	return entry, true
}

// TestRegistryMatchesCodeSpecs 钉住「Go 读取规格 == 名册」。
//
// 本测试只从 configSpecs 取名册键与代码值，名册从嵌入字节读。
// 因此单独改 config.go 的缺省值或域会红；单独改名册也会红。
func TestRegistryMatchesCodeSpecs(t *testing.T) {
	reg := loadRegistry(t)
	specs := configSpecs()
	// 2026-11-05 实测 30 条。下限取实测值：删一条规格即红。
	if len(specs) < 30 {
		t.Fatalf("规格表只有 %d 条（实测 30）——删规格即拒判", len(specs))
	}
	for _, spec := range specs {
		entry, ok := entryOf(t, reg, spec.key)
		if !ok {
			t.Errorf("%s 在名册中缺失：Go 有读取点，名册没有对照", spec.key)
			continue
		}
		compareDefault(t, spec, entry)
		compareDomain(t, spec, entry)
	}
}

// compareDefault 比较一个键的代码缺省与名册缺省。
//
// 名册 default 为 null（无缺省）时，代码侧只接受空串兜底：
// 空串在读取口径里等同"未设"，与 null 同义。
func compareDefault(t *testing.T, spec keySpec, entry registryEntry) {
	t.Helper()
	if !entry.hasDefault() {
		if s, ok := spec.fallback.(string); ok && s == "" {
			return
		}
		t.Errorf("%s 名册没有缺省值，但代码用 %#v 兜底", spec.key, spec.fallback)
		return
	}
	switch want := spec.fallback.(type) {
	case int:
		var got float64
		if err := json.Unmarshal(entry.Default, &got); err != nil {
			t.Errorf("%s 名册缺省不是数字: %v", spec.key, err)
			return
		}
		if got != float64(want) {
			t.Errorf("%s 缺省漂移：名册 %v，代码 %d", spec.key, got, want)
		}
	case float64:
		var got float64
		if err := json.Unmarshal(entry.Default, &got); err != nil {
			t.Errorf("%s 名册缺省不是数字: %v", spec.key, err)
			return
		}
		if got != want {
			t.Errorf("%s 缺省漂移：名册 %v，代码 %v", spec.key, got, want)
		}
	case bool:
		var got bool
		if err := json.Unmarshal(entry.Default, &got); err != nil {
			t.Errorf("%s 名册缺省不是布尔: %v", spec.key, err)
			return
		}
		if got != want {
			t.Errorf("%s 缺省漂移：名册 %v，代码 %v", spec.key, got, want)
		}
	case string:
		var got string
		if err := json.Unmarshal(entry.Default, &got); err != nil {
			t.Errorf("%s 名册缺省不是字符串: %v", spec.key, err)
			return
		}
		if got != want {
			t.Errorf("%s 缺省漂移：名册 %q，代码 %q", spec.key, got, want)
		}
	default:
		t.Errorf("%s 的代码缺省类型 %T 不受支持", spec.key, spec.fallback)
	}
}

// compareDomain 比较一个键的代码域与名册域。
// 代码无域（domain == nil 且 enum == nil）时名册也不得登记域：多出的域会让 doctor
// 按名册判值，而启动按代码收值，两套答案。
func compareDomain(t *testing.T, spec keySpec, entry registryEntry) {
	t.Helper()
	if len(spec.enum) > 0 {
		if !entry.hasDomain() {
			t.Errorf("%s 名册没有登记字符串域 %v", spec.key, spec.enum)
			return
		}
		var got []string
		if err := json.Unmarshal(entry.Domain, &got); err != nil {
			t.Errorf("%s 名册域不是字符串数组: %v", spec.key, err)
			return
		}
		want := append([]string(nil), spec.enum...)
		sort.Strings(got)
		sort.Strings(want)
		if strings.Join(got, ",") != strings.Join(want, ",") {
			t.Errorf("%s 闭集漂移：名册 %v，代码 %v", spec.key, got, want)
		}
		return
	}
	if spec.domain == nil {
		if entry.hasDomain() {
			t.Errorf("%s 名册登记了域 %s，代码无域", spec.key, entry.Domain)
		}
		return
	}
	var got []float64
	if err := json.Unmarshal(entry.Domain, &got); err != nil || len(got) != 2 {
		t.Errorf("%s 名册域 = %s，want 两元素数值域", spec.key, entry.Domain)
		return
	}
	if got[0] != spec.domain[0] || got[1] != spec.domain[1] {
		t.Errorf("%s 域漂移：名册 %v，代码 %v", spec.key, got, spec.domain)
	}
}

// TestRegistryMatchesSpecialKeys 钉住不经过数值规格表的三类键：
// 闭集环境名、按环境取值的不安全 cookie、必填项。
func TestRegistryMatchesSpecialKeys(t *testing.T) {
	reg := loadRegistry(t)

	var env struct {
		Default json.RawMessage `json:"default"`
		Domain  []string        `json:"domain"`
	}
	rawEnv, ok := reg["YIBAN_ENV"]
	if !ok {
		t.Fatal("YIBAN_ENV 在名册中缺失")
	}
	if err := json.Unmarshal(rawEnv, &env); err != nil {
		t.Fatalf("解析 YIBAN_ENV 条目失败: %v", err)
	}
	if len(env.Default) > 0 && string(env.Default) != "null" {
		t.Errorf("YIBAN_ENV 名册缺省 = %s，want null（缺省无：不写即启动错误）", env.Default)
	}
	got := append([]string(nil), env.Domain...)
	sort.Strings(got)
	want := make([]string, 0, len(knownEnvs))
	for name := range knownEnvs {
		want = append(want, name)
	}
	sort.Strings(want)
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Errorf("YIBAN_ENV 闭集漂移：名册 %v，代码 %v", got, want)
	}

	for _, key := range []string{"YIBAN_HASH_PEPPER", "YIBAN_CREDENTIAL_KEY"} {
		entry, ok := entryOf(t, reg, key)
		if !ok {
			t.Errorf("%s 在名册中缺失", key)
			continue
		}
		if entry.hasDefault() {
			t.Errorf("%s 名册登记了缺省 %s，但它是必填项", key, entry.Default)
		}
	}

	// 不安全 cookie 的代码缺省随环境变；名册登记的是基线（false）。
	src := baseSource()
	src["YIBAN_ENV"] = "staging"
	cfg, err := LoadFrom(src)
	if err != nil {
		t.Fatalf("staging 缺省: %v", err)
	}
	entry, ok := entryOf(t, reg, "YIBAN_INSECURE_COOKIES")
	if !ok {
		t.Fatal("YIBAN_INSECURE_COOKIES 在名册中缺失")
	}
	if !entry.hasDefault() {
		t.Fatal("YIBAN_INSECURE_COOKIES 名册没有缺省")
	}
	var wantInsecure bool
	if err := json.Unmarshal(entry.Default, &wantInsecure); err != nil {
		t.Fatalf("YIBAN_INSECURE_COOKIES 名册缺省不是布尔: %v", err)
	}
	if cfg.InsecureCookies != wantInsecure {
		t.Errorf("YIBAN_INSECURE_COOKIES 基线漂移：名册 %v，staging 代码 %v",
			wantInsecure, cfg.InsecureCookies)
	}
}

// specialRegistryKeys 是不进数值规格表的键：它们有专门的判据。
var specialRegistryKeys = []string{
	"YIBAN_ENV",
	"YIBAN_INSECURE_COOKIES",
	"YIBAN_HASH_PEPPER",
	"YIBAN_CREDENTIAL_KEY",
}

// TestEveryConfigReadHasRegistryComparison 钉住覆盖面：
// config.go 里出现的每个键字面量都必须进对照表。
//
// 没有本测试，新加一个读取点就能绕过名册对照；
// 门看的东西必须等于实际读的东西。
//
// 已知限制：只认引号字面量（双引号与反引号）。用变量拼出的键名
// （"YIBAN_" + name）不在扫描面内。
func TestEveryConfigReadHasRegistryComparison(t *testing.T) {
	body := readFile(t, "config.go")
	covered := map[string]bool{}
	for _, spec := range configSpecs() {
		covered[spec.key] = true
	}
	for _, key := range specialRegistryKeys {
		covered[key] = true
	}
	reads := keyLiteral.FindAllString(body, -1)
	if len(reads) < 8 {
		t.Fatalf("config.go 只扫到 %d 个键字面量——扫描面塌了即拒判", len(reads))
	}
	for _, raw := range reads {
		key := strings.Trim(raw, "`\"")
		if !covered[key] {
			t.Errorf("%s 出现在 config.go，但不在对照表——新读取点必须同时进名册对照", key)
		}
	}
}

// keyLiteral 匹配整段被引号包住的 YIBAN_* 键名。
var keyLiteral = regexp.MustCompile("[`\"]YIBAN_[A-Z0-9_]+[`\"]")

// scopeSets 把名册键按 scope 分成 server 与 cli 两组。
// 嵌入名册的每条都必须显式写 scope：靠缺省会让「settings 只列 server」
// 变成实现细节，名册少写一个 scope 就静默改变设置页的键面。
func scopeSets(t *testing.T, reg map[string]json.RawMessage) (server, cli map[string]bool) {
	t.Helper()
	server, cli = map[string]bool{}, map[string]bool{}
	for key, raw := range reg {
		if key == "_meta" {
			continue
		}
		var item struct {
			Scope string `json:"scope"`
		}
		if err := json.Unmarshal(raw, &item); err != nil {
			t.Fatalf("解析 %s 的 scope 失败: %v", key, err)
		}
		switch item.Scope {
		case "server":
			server[key] = true
		case "cli":
			cli[key] = true
		default:
			t.Fatalf("%s 的 scope = %q，必须是显式的 server 或 cli", key, item.Scope)
		}
	}
	return server, cli
}

// TestRegistryEqualsServerKeySurface 是双向门：Go 读到的键 ⇔ 名册键，按 scope 分区。
//
// 扫描面是 server 树的全部生产 Go 文件（含 cmd/cli 与 cmd/bench）。
// 四个方向都判：
//   - 任何 Go 读取点（全集）而名册没登记 → doctor 误报未登记键；
//   - 名册登记而代码不读 → 名册退化成旧键清单，模板会诱导部署者配无效键；
//   - server 键必须由 internal/adapters/config 读取（服务进程唯一的读点）；
//   - cli 键必须出现在 cmd/cli 或 cmd/bench（运维工具自己的读取点）。
func TestRegistryEqualsServerKeySurface(t *testing.T) {
	reg := loadRegistry(t)
	serverKeys, cliKeys := scopeSets(t, reg)
	root := filepath.Join("..", "..", "..")
	const loaderRel = "internal/adapters/config/config.go"
	inCode := map[string]bool{}
	cliCode := map[string]bool{}
	loaderKeys := map[string]bool{}
	for _, path := range productionGoFiles(t) {
		rel, err := filepath.Rel(root, path)
		if err != nil {
			t.Fatalf("rel %s: %v", path, err)
		}
		relSlash := filepath.ToSlash(rel)
		inCLI := strings.HasPrefix(relSlash, "cmd/cli/") || strings.HasPrefix(relSlash, "cmd/bench/")
		for _, raw := range keyLiteral.FindAllString(readFile(t, path), -1) {
			key := strings.Trim(raw, "`\"")
			inCode[key] = true
			if inCLI {
				cliCode[key] = true
			}
		}
		if relSlash == loaderRel {
			for _, raw := range keyLiteral.FindAllString(readFile(t, path), -1) {
				loaderKeys[strings.Trim(raw, "`\"")] = true
			}
		}
	}
	if len(inCode) < 36 || len(serverKeys)+len(cliKeys) < 36 {
		t.Fatalf("扫描面塌了：代码 %d 键 / 名册 %d 键", len(inCode), len(serverKeys)+len(cliKeys))
	}
	if len(serverKeys) == 0 || len(cliKeys) == 0 {
		t.Fatalf("scope 分区塌了：server %d / cli %d", len(serverKeys), len(cliKeys))
	}
	for key := range inCode {
		if !serverKeys[key] && !cliKeys[key] {
			t.Errorf("%s 被 Go 读取，但不在嵌入名册内", key)
		}
	}
	for key := range serverKeys {
		if !inCode[key] {
			t.Errorf("%s（scope=server）在嵌入名册内，但没有任何读取点", key)
		}
		if !loaderKeys[key] {
			t.Errorf("%s（scope=server）必须由 internal/adapters/config 读取，那里没有它的字面量", key)
		}
	}
	for key := range cliKeys {
		if !inCode[key] {
			t.Errorf("%s（scope=cli）在嵌入名册内，但没有任何读取点", key)
		}
		if !cliCode[key] {
			t.Errorf("%s（scope=cli）必须出现在 cmd/cli 或 cmd/bench，服务进程不得读它", key)
		}
	}
	for key := range loaderKeys {
		if !serverKeys[key] {
			t.Errorf("internal/adapters/config 读到 %s，但它的 scope 不是 server", key)
		}
	}
}

// TestRegistrySensitiveMatchesCode 钉住名册的 sensitive 标记与代码口径。
//
// 设置页的掩码完全由名册驱动：名册漏标一个密钥，GET /api/admin/settings
// 就会回显原文。server 键两侧逐键相等：名册多标或少标都红。
// scope=cli 的键不进设置页，掩码不适用，sensitive 只作文档，不在此比对。
func TestRegistrySensitiveMatchesCode(t *testing.T) {
	reg := loadRegistry(t)
	serverKeys, _ := scopeSets(t, reg)
	want := map[string]bool{}
	for _, key := range sensitiveKeys {
		want[key] = true
	}
	if len(want) == 0 {
		t.Fatal("代码侧敏感表为空——删空即拒判")
	}
	got := map[string]bool{}
	for key, raw := range reg {
		if key == "_meta" || !serverKeys[key] {
			continue
		}
		var item struct {
			Sensitive bool `json:"sensitive"`
		}
		if err := json.Unmarshal(raw, &item); err != nil {
			t.Fatalf("解析 %s 的 sensitive 失败: %v", key, err)
		}
		if item.Sensitive {
			got[key] = true
		}
	}
	for key := range want {
		if !got[key] {
			t.Errorf("%s 是代码认定的敏感键，名册没有标 sensitive", key)
		}
	}
	for key := range got {
		if !want[key] {
			t.Errorf("%s 在名册里标了 sensitive，代码侧敏感表没有它", key)
		}
	}
}

// TestGoStackDoesNotReadLegacyRoster 钉住旧名册的边界。
//
// 旧 Python 栈的 config/registry.json（187 键）继续服务旧线，但 Go 生产代码
// 不得引用它：读旧文件等于把旧键混进新栈的键面。
func TestGoStackDoesNotReadLegacyRoster(t *testing.T) {
	for _, path := range productionGoFiles(t) {
		if strings.Contains(readFile(t, path), "config/registry.json") {
			t.Errorf("%s 引用了旧名册 config/registry.json；Go 栈读嵌入名册", path)
		}
	}
	// 旧名册仍须留在仓库里（旧线在读），并保持比新名册大得多的键面。
	legacy := readFile(t, filepath.Join("..", "..", "..", "..", "config", "registry.json"))
	var old map[string]json.RawMessage
	if err := json.Unmarshal([]byte(legacy), &old); err != nil {
		t.Fatalf("旧名册解析失败: %v", err)
	}
	if len(old) < 150 {
		t.Fatalf("旧名册只剩 %d 条——旧线仍在读它，删键会让旧线判红", len(old))
	}
	reg := loadRegistry(t)
	for _, key := range []string{"YIBAN_ENV_FILE", "YIBAN_WINDOW_EDGE_SEC", "YIBAN_MAIL_ENABLE"} {
		if _, ok := reg[key]; ok {
			t.Errorf("%s 是旧线专用键，不得进入 Go 名册", key)
		}
	}
}

// productionGoFiles 列出 server 树的全部生产 Go 文件（含 cmd/cli 与 cmd/bench：
// 名册的 scope=cli 键由它们读取，门按 scope 分区校验落点）。
// 2026-11-05 实测 170+ 个；少于 150 视为扫描面塌陷。
func productionGoFiles(t *testing.T) []string {
	t.Helper()
	root := filepath.Join("..", "..", "..")
	var out []string
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			switch d.Name() {
			case ".git", "node_modules", "testdata":
				return filepath.SkipDir
			}
			return nil
		}
		if strings.HasSuffix(d.Name(), ".go") && !strings.HasSuffix(d.Name(), "_test.go") {
			out = append(out, path)
		}
		return nil
	})
	if err != nil {
		t.Fatalf("扫描生产 Go 源失败: %v", err)
	}
	if len(out) < 150 {
		t.Fatalf("只扫到 %d 个生产 Go 文件——扫描面塌了即拒判", len(out))
	}
	return out
}

// readFile 读一个 UTF-8 文本文件。
func readFile(t *testing.T, path string) string {
	t.Helper()
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("读 %s 失败: %v", path, err)
	}
	return string(body)
}

// templateKeyLines 解析一份 env 模板：去掉空行与注释，返回键→值。
// 值与键都不做 shell 展开：模板只允许 KEY=VALUE 的直白形状。
func templateKeyLines(t *testing.T, path string) map[string]string {
	t.Helper()
	out := map[string]string{}
	for _, line := range strings.Split(readFile(t, path), "\n") {
		line = strings.TrimSpace(strings.TrimSuffix(line, "\r"))
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			t.Fatalf("%s: 非 KEY=VALUE 行: %q", path, line)
		}
		key = strings.TrimSpace(key)
		if !templateKey.MatchString(key) {
			t.Fatalf("%s: 非法键名 %q", path, key)
		}
		if _, dup := out[key]; dup {
			t.Fatalf("%s: 重复键 %s", path, key)
		}
		out[key] = strings.TrimSpace(value)
	}
	return out
}

// templateKey 是 env 模板允许的键名形状。
var templateKey = regexp.MustCompile(`^[A-Z][A-Z0-9_]*$`)

// composeDerivedKey 由 compose 文件从 POSTGRES_PASSWORD 派生，不写进容器模板：
// 口令只有一处来源，避免同一份密钥在模板里出现两遍。
var composeDerivedKey = "YIBAN_DATABASE_URL"

// composeOnlyKeys 是容器编排层自己的变量，不是 Go 服务端配置。
var composeOnlyKeys = map[string]bool{"POSTGRES_PASSWORD": true}

// TestTemplatesMatchRegistry 钉住三份 env 模板的键面。
//
// 模板键面 == scope=server 的名册键（容器模板的 DSN 由 compose 派生，见
// composeDerivedKey）；scope=cli 的键是运维 CLI 专用，写进模板会诱导部署者
// 把 backup 口令放进服务进程环境，故显式禁止。
// 秘密键在模板里必须为空：仓库里不得出现任何真实值或占位口令。
func TestTemplatesMatchRegistry(t *testing.T) {
	reg := loadRegistry(t)
	serverKeys, cliKeys := scopeSets(t, reg)
	cases := []struct {
		name string
		path string
		// derived 是该模板允许缺席、但由编排文件派生的键。
		derived map[string]bool
		// extras 是该模板允许出现的非名册键。
		extras map[string]bool
	}{
		{
			name: "development",
			path: filepath.Join("..", "..", "..", ".env.example"),
		},
		{
			name: "baremetal",
			path: filepath.Join("..", "..", "..", "..", "deploy", "baremetal", "server.env.example"),
		},
		{
			name:    "container",
			path:    filepath.Join("..", "..", "..", "..", "deploy", "container", "compose.env.example"),
			derived: map[string]bool{composeDerivedKey: true},
			extras:  composeOnlyKeys,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			keys := templateKeyLines(t, tc.path)
			if len(keys) == 0 {
				t.Fatalf("%s 没有解析到任何键", tc.path)
			}
			for key := range keys {
				switch {
				case serverKeys[key]:
				case cliKeys[key]:
					t.Errorf("%s 出现 scope=cli 的键 %s：CLI 专用键不得进运行配置模板", tc.path, key)
				case tc.extras[key]:
				default:
					t.Errorf("%s 出现名册外的键 %s", tc.path, key)
				}
			}
			for key := range serverKeys {
				if tc.derived[key] {
					continue
				}
				if _, ok := keys[key]; !ok {
					t.Errorf("%s 缺名册键 %s", tc.path, key)
				}
			}
			// 秘密键与非名册口令键一律留空。
			for key := range keys {
				if tc.extras[key] {
					if keys[key] != "" {
						t.Errorf("%s: %s 必须留空，不得写入仓库", tc.path, key)
					}
					continue
				}
				var item struct {
					Sensitive bool `json:"sensitive"`
				}
				if err := json.Unmarshal(reg[key], &item); err != nil {
					t.Fatalf("解析 %s 的 sensitive 失败: %v", key, err)
				}
				if item.Sensitive && keys[key] != "" {
					t.Errorf("%s: 秘密键 %s 有值 %q——模板不得含密钥", tc.path, key, keys[key])
				}
			}
		})
	}
}

// TestContainerComposeDerivesDSN 钉住容器模板的两个例外：
// YIBAN_DATABASE_URL 不在 compose.env 里，由 compose 从 POSTGRES_PASSWORD 派生；
// YIBAN_TRUSTED_PROXIES 走插值透传，export 覆盖才不会静默失效。
// 没有本测试，模板缺一个必填键或文档命令漏写 --env-file 都会静默通过键面门。
func TestContainerComposeDerivesDSN(t *testing.T) {
	compose := readFile(t, filepath.Join("..", "..", "..", "..", "deploy", "container", "docker-compose.yml"))
	for _, want := range []string{
		"YIBAN_DATABASE_URL:",
		"${POSTGRES_PASSWORD",
		"YIBAN_TRUSTED_PROXIES:",
		"${YIBAN_TRUSTED_PROXIES:-",
		"env_file:",
		"compose.env",
		"--env-file",
	} {
		if !strings.Contains(compose, want) {
			t.Errorf("docker-compose.yml 缺少 %q：容器必须显式加载 compose.env、派生 DSN 并透传可信代理", want)
		}
	}
}
