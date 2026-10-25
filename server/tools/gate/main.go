package main

import (
	"flag"
	"fmt"
	"os"
	"sort"
)

func main() {
	root := flag.String("root", ".", "root directory to scan")
	rulesPath := flag.String("rules", "canonical.tsv", "canonical rules file")
	includeTests := flag.Bool("tests", false, "scan _test.go files too")
	flag.Parse()
	os.Exit(run(*root, *rulesPath, *includeTests))
}

// run 返回进程退出码。
// 它与 main 分离，使测试能钉住拒绝路径，而无需另起进程。
func run(root, rulesPath string, includeTests bool) int {
	rules, err := LoadRules(rulesPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "gate:", err)
		return 2
	}
	scanned, err := CountGoFiles(root, includeTests)
	if err != nil {
		fmt.Fprintln(os.Stderr, "gate:", err)
		return 2
	}
	if scanned == 0 {
		fmt.Fprintln(os.Stderr, "gate: no Go files under root; refused to pass")
		return 2
	}
	if err := ValidateCheckedScopes(root, rules); err != nil {
		fmt.Fprintln(os.Stderr, "gate:", err)
		return 2
	}
	violations, err := Scan(root, rules, includeTests)
	if err != nil {
		fmt.Fprintln(os.Stderr, "gate:", err)
		return 2
	}
	sort.Slice(violations, func(i, j int) bool {
		if violations[i].File != violations[j].File {
			return violations[i].File < violations[j].File
		}
		return violations[i].Line < violations[j].Line
	})
	for _, v := range violations {
		fmt.Printf("%s:%d: %s: %s\n", v.File, v.Line, v.Rule.ID, v.Rule.Message)
	}
	if len(violations) > 0 {
		fmt.Fprintf(os.Stderr, "gate: %d violation(s)\n", len(violations))
		return 1
	}
	fmt.Printf("gate: ok (%d rules)\n", len(rules))
	return 0
}
