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

// run returns the process exit code. It is separate from main so a test can
// pin the refusal paths without spawning a process.
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
