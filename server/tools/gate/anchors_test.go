package main

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"path/filepath"
	"strings"
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
	// pattern 存在、owner 不存在：单独钉住 owner 检查，删掉它会红。
	writeFile(t, root, "internal/app/a.go", `package app
import "time"
var _ = time.Now()
`)
	rules := []Rule{testRule("clock.now", "call", "internal/core/clock/", "time.Now", "")}
	err := ValidateRuleAnchors(root, rules, false)
	if err == nil {
		t.Fatal("rule whose owner matches no file must fail")
	}
	if !strings.Contains(err.Error(), "owner") {
		t.Fatalf("错误必须点名 owner：%v", err)
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
		"boundary.entry", "boundary.signing", "boundary.assembly",
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

// 规则整行快照：kind/owner/pattern/allow 的改动同样必须是一次显式决定。
// 只钉 ID 会让「call 改 forbid」「pattern 改错」静默通过（r4c 复审 P2-1/P2-2）。
// 哈希变了就确认改动，然后更新本常量。
func TestCanonicalRuleRowsPinned(t *testing.T) {
	rules, err := LoadRules(filepath.Join("..", "..", "canonical.tsv"))
	if err != nil {
		t.Fatalf("load canonical: %v", err)
	}
	h := sha256.New()
	for _, r := range rules {
		fmt.Fprintf(h, "%s\t%s\t%s\t%s\t%s\t%s\n", r.ID, r.Kind, r.Owner, r.Pattern, r.Allow, r.Message)
	}
	got := hex.EncodeToString(h.Sum(nil))
	// rb3：boundary.adapters 的 allow 增列
	// internal/adapters/pg/usecase_e2e_test.go（用例层 e2e 需要真实 PG 适配器）。
	const want = "72be2bddc091a698c2bd847d5b1ed30dc403c8d4fe779a95c7a2441004aca8e6"
	if got != want {
		t.Fatalf("canonical 规则内容变了（hash %s）；确认改动后更新 TestCanonicalRuleRowsPinned 的哈希", got)
	}
}

// anchors 的 includeTests=false 路径：只看生产文件时，仅测试里出现的
// pattern 应判为「没有落点」；includeTests=true 时才找到。
func TestValidateRuleAnchorsIncludeTests(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "internal/core/clock/clock_test.go", `package clock
import "time"
var _ = time.Now()
`)
	rules := []Rule{testRule("clock.now", "call", "internal/core/clock/", "time.Now", "")}
	if err := ValidateRuleAnchors(root, rules, false); err == nil {
		t.Fatal("不含测试文件时，只有测试落点的规则必须判无落点")
	}
	if err := ValidateRuleAnchors(root, rules, true); err != nil {
		t.Fatalf("含测试文件时应找到落点: %v", err)
	}
}
