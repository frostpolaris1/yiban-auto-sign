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

// registryPath 是配置名册相对本包的路径。
// 测试直接读名册文件，不复制任何缺省值字面量：
// 名册改值而代码不改、或代码改值而名册不改，都必须红。
const registryPath = "../../../../config/registry.json"

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

// loadRegistry 读回名册的原始条目。
// 逐条解析而不是整表解析：YIBAN_ENV 的域是字符串闭集，整表按数值域解会失败。
func loadRegistry(t *testing.T) map[string]json.RawMessage {
	t.Helper()
	body, err := os.ReadFile(registryPath)
	if err != nil {
		t.Fatalf("读名册失败: %v", err)
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
// 本测试只从 configSpecs 取名册键与代码值，名册从文件读。
// 因此单独改 config.go 的缺省值或域会红；单独改名册也会红。
func TestRegistryMatchesCodeSpecs(t *testing.T) {
	reg := loadRegistry(t)
	specs := configSpecs()
	// 2026-10-27 实测 11 条。下限取实测值：删一条规格即红。
	if len(specs) < 11 {
		t.Fatalf("规格表只有 %d 条（实测 11）——删规格即拒判", len(specs))
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
// （"YIBAN_" + name）不在扫描面内；与 check-config-registry.py 同一已知弱化点。
func TestEveryConfigReadHasRegistryComparison(t *testing.T) {
	body, err := os.ReadFile("config.go")
	if err != nil {
		t.Fatalf("读 config.go 失败: %v", err)
	}
	covered := map[string]bool{}
	for _, spec := range configSpecs() {
		covered[spec.key] = true
	}
	for _, key := range specialRegistryKeys {
		covered[key] = true
	}
	quoted := regexp.MustCompile("[`\"]YIBAN_[A-Z0-9_]+[`\"]")
	reads := quoted.FindAllString(string(body), -1)
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

// legacyNonGoKeys 是名册登记、但 Go 服务端有意不读的旧键。
//
// 本表是显式豁免（AGENTS §28）：键从名册消失会让仍读它的 Python 旧线判红；
// Go 若开始读它，本测试也会红，迫使交接时处理新旧键优先级。
// 当前只有 YIBAN_WINDOW_EDGE_SEC：Python 旧线按前后对称裁剪消费，
// Go 线只用 FRONT/BACK 两个新键。
var legacyNonGoKeys = []string{"YIBAN_WINDOW_EDGE_SEC"}

// TestLegacyKeysAreRegisteredButNotReadByGo 钉住旧键的豁免边界。
// 扫描面是整个 server 生产树（不含 _test.go）：包外引用同样判红。
func TestLegacyKeysAreRegisteredButNotReadByGo(t *testing.T) {
	reg := loadRegistry(t)
	if len(legacyNonGoKeys) == 0 {
		t.Fatal("豁免表为空——删空即拒判")
	}
	files := productionGoFiles(t)
	for _, key := range legacyNonGoKeys {
		if _, ok := reg[key]; !ok {
			t.Errorf("%s 不在名册：Python 旧线仍在读它，删名册会让旧线判红", key)
		}
		for _, path := range files {
			body, err := os.ReadFile(path)
			if err != nil {
				t.Fatalf("读 %s 失败: %v", path, err)
			}
			text := string(body)
			if strings.Contains(text, `"`+key+`"`) || strings.Contains(text, "`"+key+"`") {
				t.Errorf("%s 被 Go 生产代码引用（%s）：从豁免表删除，并处理与 FRONT/BACK 新键的优先级", key, path)
			}
		}
	}
}

// productionGoFiles 列出 server 根下的生产 Go 文件（不含 _test.go）。
// cwd = server/internal/adapters/config；server 根是三跳。
// 2026-10-27 实测 152 个；少于 150 视为扫描面塌陷。
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
