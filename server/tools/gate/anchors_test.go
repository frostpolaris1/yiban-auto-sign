package main

import (
	"path/filepath"
	"testing"
)

// ValidateRuleAnchors：owner 或 pattern 改名后，规则不再检查任何东西。
// 门必须把这种空转变为 rc=2（AGENTS §29）。
func TestValidateRuleAnchorsAccepts(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "internal/core/clock/clock.go", `package clock
import "time"
func Now() time.Time { return time.Now() }
`)
	rules := []Rule{testRule("clock.now", "call", "internal/core/clock/", "time.Now", "")}
	if err := ValidateRuleAnchors(root, rules, false); err != nil {
		t.Fatalf("live rule must pass: %v", err)
	}
}

func TestValidateRuleAnchorsRejectsMissingPattern(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "internal/core/clock/clock.go", `package clock
import "time"
func Now() time.Time { return time.Now() }
`)
	// 规则名改了，但树里没有对应调用：规则永远不会触发。
	rules := []Rule{testRule("clock.now", "call", "internal/core/clock/", "time.NowRenamed", "")}
	if err := ValidateRuleAnchors(root, rules, false); err == nil {
		t.Fatal("rule whose pattern is absent must fail")
	}
}

func TestValidateRuleAnchorsRejectsVacuousOwner(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "internal/app/a.go", `package app
var _ = 1
`)
	// owner 目录被删/改名：规则检查零个文件。
	rules := []Rule{testRule("clock.now", "call", "internal/core/clock/", "time.Now", "")}
	if err := ValidateRuleAnchors(root, rules, false); err == nil {
		t.Fatal("rule whose owner matches no file must fail")
	}
}

func TestValidateRuleAnchorsRejectsRenamedStringValue(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "internal/core/status/status.go", `package status
const Done = "finished"
`)
	rules := []Rule{testRule("status.literal", "string", "internal/core/status/", "done", "")}
	if err := ValidateRuleAnchors(root, rules, false); err == nil {
		t.Fatal("string rule whose only value was renamed must fail")
	}
}

// 真实 canonical.tsv 的每条规则都必须仍有落点。
// 这条测试跑在 -root .. 上，等于每次 go test 都复核一遍规则锚点。
func TestCanonicalRulesAnchoredInRepo(t *testing.T) {
	root := filepath.Join("..", "..")
	rules, err := LoadRules(filepath.Join(root, "canonical.tsv"))
	if err != nil {
		t.Fatalf("load canonical: %v", err)
	}
	if err := ValidateRuleAnchors(root, rules, true); err != nil {
		t.Fatalf("canonical anchors: %v", err)
	}
}

// 规则 ID 清单是钉子：删规则、改规则名都必须同步改这里。
// 删除一条规则应当是一次显式决定，不是一次静默的绿。
func TestCanonicalRuleIDsPinned(t *testing.T) {
	want := []string{
		"clock.now", "clock.location", "clock.tz",
		"env.get", "env.lookup",
		"status.literal", "errors.http",
		"mask.phone", "mask.email", "mask.device", "mask.text",
		"capacity.func", "retry.type", "egress.func",
		"plan.slot", "image.prepare",
		"boundary.core", "boundary.app", "boundary.adapters",
		"boundary.entry", "boundary.signing",
		"cookie.set",
		"kdf.argon.idkey", "kdf.argon.id",
		"token.csrf.func", "token.new.func",
		"fake.import",
		"email.normalize.lower",
		"window.validate", "window.effective",
		"rate.bucket", "rate.limiter",
		"plan.nearest", "plan.spread", "plan.phase",
		"retry.budgetfor", "retry.nextdelay", "plan.spreadwithin",
	}
	rules, err := LoadRules(filepath.Join("..", "..", "canonical.tsv"))
	if err != nil {
		t.Fatalf("load canonical: %v", err)
	}
	if len(rules) != len(want) {
		t.Fatalf("canonical has %d rules, pinned list has %d; add or remove must update this list",
			len(rules), len(want))
	}
	for i, rule := range rules {
		if rule.ID != want[i] {
			t.Fatalf("rule %d = %q, pinned %q; rename must be an explicit decision",
				i, rule.ID, want[i])
		}
	}
}
