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

	rules, err := LoadRules(*rulesPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "gate:", err)
		os.Exit(2)
	}
	violations, err := Scan(*root, rules, *includeTests)
	if err != nil {
		fmt.Fprintln(os.Stderr, "gate:", err)
		os.Exit(2)
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
		os.Exit(1)
	}
	fmt.Printf("gate: ok (%d rules)\n", len(rules))
}
