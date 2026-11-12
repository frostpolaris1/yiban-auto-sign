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
// 门必须把这种空转变为 rc=2。
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
		"status.literal", "errors.http", "fs.os.impl",
		"mask.phone", "mask.email", "mask.device", "mask.text",
		"capacity.func", "retry.type", "egress.func",
		"plan.slot", "image.prepare",
		"boundary.core", "boundary.app", "boundary.adapters",
		"boundary.entry", "boundary.signing",
		"boundary.core.protocol", "boundary.core.contract",
		"boundary.app.protocol", "boundary.entry.protocol",
		"boundary.assembly",
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
// 只钉 ID 会让「call 改 forbid」「pattern 改错」静默通过。
// 哈希变了就确认改动，然后更新本常量。
func TestCanonicalRuleRowsPinned(t *testing.T) {
	rules, err := LoadRules(filepath.Join("..", "..", "canonical.tsv"))
	if err != nil {
		t.Fatalf("load canonical: %v", err)
	}
	h := sha256.New()
	for _, r := range rules {
		_, _ = fmt.Fprintf(h, "%s\t%s\t%s\t%s\t%s\t%s\n", r.ID, r.Kind, r.Owner, r.Pattern, r.Allow, r.Message)
	}
	got := hex.EncodeToString(h.Sum(nil))
	// rb3：boundary.adapters 的 allow 增列
	// internal/adapters/pg/usecase_e2e_test.go（用例层 e2e 需要真实 PG 适配器）。
	// db1：allow 再增列 internal/adapters/pg/credential_state_e2e_test.go
	// （凭据错结算与恢复的用例层 e2e 同样需要真实 PG 适配器）。
	// user-groups：status.literal allow 增列 internal/core/user/
	// （用户展示分组的 pending 是分组词，不是任务状态）。
	// update-u1：env.get allow 增列 cmd/cli/selfcheck_pg_e2e_test.go
	// （自检真库 e2e 沿用既有 os.Getenv 读测试 DSN 的口径）。
	// update-u2：env.get 与 status.literal 的 allow 增列
	// cmd/cli/update_u2_e2e_test.go（干跑 update.sh 的 PATH 沿用既有口径；
	// 状态文件里的 success/failed 是更新结果的词表，不是任务状态）。
	// parity-be-a2：boundary.adapters allow 增列
	// internal/adapters/pg/settings_e2e_test.go（订阅/公告/审计列表的用例层 e2e）。
	// status.literal allow 增列 internal/core/notify/
	// （投递账本的 pending/sent/failed/skipped 是投递状态，不是任务状态）。
	// phc：clock.now allow 增列 cmd/server/phc_reauth_e2e_test.go
	// （重置令牌播种沿用 auth_e2e_test.go 的真实墙钟口径）。
	// p12：env.get allow 增列 cmd/server/p12_wait_target_e2e_test.go
	// （等待型重扫真库 e2e 沿用 loop_e2e_test.go 的 os.Getenv 读测试 DSN 口径）。
	// ykxf：clock.now allow 增列 cmd/server/ykxf_e2e_test.go
	// （探测/自动核验 e2e 用真实墙钟做有界轮询）；
	// status.literal allow 增列 cmd/server/ykxf_e2e_test.go 与
	// internal/app/account/auto_approve_test.go（账号状态词 pending/active
	// 来自 core/account，测试断言按该词表书写）。
	// 3a：env.get allow 增列 cmd/server/engine_pause_e2e_test.go
	// （全局暂停真库 e2e 沿用 loop/p12_wait_target 的 os.Getenv 读测试 DSN 口径）。
	// 76t：signing 边界收口，新增 boundary.core.protocol / boundary.core.contract /
	// boundary.app.protocol / boundary.entry.protocol 四条 import 规则。
	// rlu：boundary.adapters allow 增列
	// internal/adapters/pg/schedule_capacity_e2e_test.go、
	// internal/adapters/pg/schedule_override_e2e_test.go 与
	// internal/adapters/pg/schedule_geometry_e2e_test.go
	// （容量缩容/回升、覆盖原子性与名册几何的用例层 e2e 需要真实 PG 适配器）。
	// sfr：新增 fs.os.impl forbid 规则：app 层不得实现 OS 文件系统
	// （os.CreateTemp 原子写），实现由 cmd/cli 注入（P2-b，2026-11-11）。
	const want = "1b27cc70d31c276ab1252c8b6770f2c8820683925d76223abd9efc81ded1c3a2"
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
