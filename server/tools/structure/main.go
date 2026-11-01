// Command structure 执行仓库布局检查。
//
// # 功能
// 读 structure.tsv，核对两个方向：
//   - 磁盘 → 注册表：每个受管域的直属子目录都已登记；
//   - 注册表 → 磁盘：登记的每个 token 都真实存在（? 前缀的 token 允许缺席）。
//
// 缺域除非以 ? 标记为可选，否则报错。它还执行前端命名规则。
// 导入方向由 ui/scripts/structure-gate.mjs 检查（该脚本解析模块路径）。
// 本命令**不**改文件、**不**移动目录、**不**启动服务。
//
// # 归属
// 仓库门禁件，由 scripts/verify.sh 调用。
//
// # 复用
// loadRules、checkAllowlist、checkFrontend（均为包内函数）。
//
// # 口径
// 空规则文件、空白名单、未知域、零目录扫描**一律报错**：
// 本门绝不靠「什么都没扫」通过。
package main

import (
	"bufio"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

// rule 是一个受管目录及其已登记子目录。
type rule struct {
	scope         string
	optional      bool
	allow         map[string]bool
	optionalAllow map[string]bool
	note          string
}

var (
	pageName  = regexp.MustCompile(`^[A-Z][A-Za-z0-9]*Page\.tsx$`)
	lowerName = regexp.MustCompile(`^[a-z][a-z0-9]*$`)
	skipDirs  = map[string]bool{".git": true, "node_modules": true}
)

func main() {
	root := flag.String("root", "..", "repository root")
	rulesPath := flag.String("rules", "structure.tsv", "rules file")
	flag.Parse()

	rules, err := loadRules(*rulesPath)
	if err != nil {
		fatal(err)
	}
	problems, seen, err := checkAllowlist(*root, rules)
	if err != nil {
		fatal(err)
	}
	if seen == 0 {
		fatal(fmt.Errorf("structure: no managed sub-scope exists; refused to pass"))
	}
	uiProblems, err := checkFrontend(*root)
	if err != nil {
		fatal(err)
	}
	problems = append(problems, uiProblems...)
	if len(problems) > 0 {
		sort.Strings(problems)
		for _, p := range problems {
			fmt.Fprintln(os.Stderr, p)
		}
		fatal(fmt.Errorf("structure: %d problem(s)", len(problems)))
	}
	fmt.Printf("structure: ok (%d scopes, %d rules)\n", seen, len(rules))
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, err)
	os.Exit(1)
}

// loadRules 解析 scope<TAB>allow,allow<TAB>note 行。
// 前导 ? 表示该域在本阶段可以不存在。
func loadRules(path string) ([]rule, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	var rules []rule
	sc := bufio.NewScanner(f)
	for line := 1; sc.Scan(); line++ {
		text := strings.TrimSpace(sc.Text())
		if text == "" || strings.HasPrefix(text, "#") {
			continue
		}
		parts := strings.Split(sc.Text(), "\t")
		if len(parts) < 2 {
			return nil, fmt.Errorf("%s:%d: want scope, allow and note", path, line)
		}
		scope := strings.TrimSpace(parts[0])
		optional := strings.HasPrefix(scope, "?")
		scope = strings.TrimPrefix(scope, "?")
		if !knownScope(scope) {
			return nil, fmt.Errorf("%s:%d: unknown scope %q", path, line, scope)
		}
		allow := map[string]bool{}
		optionalAllow := map[string]bool{}
		for _, name := range strings.Split(parts[1], ",") {
			name = strings.TrimSpace(name)
			if name == "" {
				continue
			}
			if strings.HasPrefix(name, "?") {
				name = strings.TrimPrefix(name, "?")
				if name == "" {
					return nil, fmt.Errorf("%s:%d: empty optional token", path, line)
				}
				optionalAllow[name] = true
				continue
			}
			allow[name] = true
		}
		for name := range allow {
			if optionalAllow[name] {
				return nil, fmt.Errorf("%s:%d: token %q is both required and optional", path, line, name)
			}
		}
		if len(allow)+len(optionalAllow) == 0 {
			return nil, fmt.Errorf("%s:%d: empty allowlist", path, line)
		}
		note := ""
		if len(parts) >= 3 {
			note = strings.TrimSpace(parts[2])
		}
		rules = append(rules, rule{scope: scope, optional: optional, allow: allow, optionalAllow: optionalAllow, note: note})
	}
	if err := sc.Err(); err != nil {
		return nil, err
	}
	if len(rules) == 0 {
		return nil, fmt.Errorf("%s: no rules; refused to pass", path)
	}
	return rules, nil
}

// knownScope 校验前两段路径，
// 使 server/internals 这类笔误被拒绝，而不是被静默跳过。
func knownScope(scope string) bool {
	if scope == "root" {
		return true
	}
	parts := strings.Split(scope, "/")
	if len(parts) < 2 {
		return false
	}
	switch parts[0] {
	case "server":
		switch parts[1] {
		case "internal", "migrations", "cmd", "contracts", "tools", "scripts":
			return true
		}
	case "ui":
		return parts[1] == "src"
	}
	return false
}

// checkAllowlist 报告两个方向的问题：
//   - 缺失的必需域与未登记的子目录；
//   - 登记了却不存在的 token（? 前缀的 token 允许缺席）。
//
// 它还统计磁盘上真实存在的域数。
func checkAllowlist(root string, rules []rule) ([]string, int, error) {
	var problems []string
	seen := 0
	for _, r := range rules {
		dir := root
		if r.scope != "root" {
			dir = filepath.Join(root, filepath.FromSlash(r.scope))
		}
		entries, err := os.ReadDir(dir)
		if err != nil {
			if os.IsNotExist(err) {
				if r.optional {
					fmt.Printf("structure: skip optional scope %s\n", r.scope)
					continue
				}
				problems = append(problems, fmt.Sprintf("%s: scope is missing (mark it optional with ? if this phase does not create it)", r.scope))
				continue
			}
			return nil, 0, err
		}
		// 根目录恒存在。
		// **只有受管子域才能证明本门确实看过东西**。
		if r.scope != "root" {
			seen++
		}
		// 反向检查：登记了却没有目录，说明注册表带着死 token。
		// 死 token 会静默放行已删目录的重建，故与「未登记目录」同权判红。
		// 确实可能缺席的运行期目录用 ? 前缀显式豁免（root 的 work/data/out）。
		present := make(map[string]bool, len(entries))
		for _, e := range entries {
			if e.IsDir() {
				present[e.Name()] = true
			}
		}
		for name := range r.allow {
			if !present[name] {
				problems = append(problems, fmt.Sprintf("%s: registered directory %q does not exist (remove it or mark it optional with ?)", r.scope, name))
			}
		}
		for _, e := range entries {
			if !e.IsDir() {
				continue
			}
			name := e.Name()
			if skipDirs[name] {
				continue
			}
			if r.scope == "root" && strings.HasPrefix(name, ".") {
				continue
			}
			if !r.allow[name] && !r.optionalAllow[name] {
				problems = append(problems, fmt.Sprintf("%s: unregistered directory %q (register it in structure.tsv)", r.scope, name))
			}
		}
	}
	return problems, seen, nil
}

// checkFrontend 执行页面命名、目录命名与导出风格检查。
func checkFrontend(root string) ([]string, error) {
	src := filepath.Join(root, "ui", "src")
	if _, err := os.Stat(src); os.IsNotExist(err) {
		return nil, nil
	} else if err != nil {
		return nil, err
	}

	var problems []string
	scanned := 0
	err := filepath.WalkDir(src, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			if skipDirs[d.Name()] {
				return filepath.SkipDir
			}
			name := d.Name()
			rel, _ := filepath.Rel(src, path)
			rel = filepath.ToSlash(rel)
			if (strings.HasPrefix(rel, "features/") || strings.HasPrefix(rel, "components/")) && !lowerName.MatchString(name) {
				problems = append(problems, fmt.Sprintf("ui/src/%s: directory name must be lowercase ascii", rel))
			}
			return nil
		}

		name := d.Name()
		if !strings.HasSuffix(name, ".ts") && !strings.HasSuffix(name, ".tsx") {
			return nil
		}
		scanned++
		rel, _ := filepath.Rel(src, path)
		rel = filepath.ToSlash(rel)

		if strings.HasPrefix(rel, "pages/") && strings.HasSuffix(name, ".tsx") && !pageName.MatchString(name) {
			problems = append(problems, fmt.Sprintf("ui/src/%s: page files must be named <Name>Page.tsx", rel))
		}

		body, readErr := os.ReadFile(path)
		if readErr != nil {
			return readErr
		}
		if strings.Contains(string(body), "export default") {
			problems = append(problems, fmt.Sprintf("ui/src/%s: use named exports, not export default", rel))
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	if scanned == 0 {
		problems = append(problems, "ui/src: no TypeScript files found; refused to pass")
	}
	return problems, nil
}
