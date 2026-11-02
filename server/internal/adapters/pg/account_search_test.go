package pg

// 账号搜索片段的守卫单测。
//
// 判据：片段里出现的最高参数编号必须等于实参个数。
// 多传一个未引用的参数会让扩展协议报参数个数不符；
// 少传则 SQL 直接失败。两类错误都不该等到 e2e 才发现。

import (
	"regexp"
	"strconv"
	"strings"
	"testing"
)

func TestAccountSearchWherePlaceholdersMatchArgs(t *testing.T) {
	cases := []string{
		"",
		"   ",
		"138",                     // 1..3 位前缀
		"0041",                    // 4 位后缀
		"13800138000",             // 11 位全号：精确直达
		"+86 138 0000 0040",       // 国家码与分隔符归一后精确直达
		"alpha.owner@example.com", // 完整邮箱：精确直达
		"123456",                  // 5..10 位：不参与号码匹配
		"a***@example.com",        // 可见掩码
		"100%",                    // LIKE 元字符按字面
		"东区宿舍",                    // 中文名称子串
	}
	for _, term := range cases {
		var args []any
		clause := accountSearchWhere(term, &args)
		if strings.TrimSpace(term) == "" {
			if clause != "" || len(args) != 0 {
				t.Fatalf("empty term produced clause %q with %d args", clause, len(args))
			}
			continue
		}
		highest := 0
		for _, match := range regexp.MustCompile(`\$(\d+)`).FindAllStringSubmatch(clause, -1) {
			index, err := strconv.Atoi(match[1])
			if err != nil {
				t.Fatalf("placeholder %q: %v", match[0], err)
			}
			if index > highest {
				highest = index
			}
		}
		if highest != len(args) {
			t.Fatalf("term %q: highest placeholder $%d, args %d; clause=%s",
				term, highest, len(args), clause)
		}
	}
}

func TestAccountSearchWhereOmitsPhoneForMiddleFragments(t *testing.T) {
	var args []any
	clause := accountSearchWhere("123456", &args)
	if strings.Contains(clause, "phone LIKE") || strings.Contains(clause, "phone =") {
		t.Fatalf("5..10 digit fragment produced a phone clause: %s", clause)
	}
	if len(args) != 1 {
		t.Fatalf("args = %d, want only the text term", len(args))
	}
}

func TestAccountSearchWhereExactIdentifiers(t *testing.T) {
	var args []any
	clause := accountSearchWhere("13800138000", &args)
	if !strings.Contains(clause, "phone = $1") || len(args) != 1 || args[0] != "13800138000" {
		t.Fatalf("complete phone clause = %q args=%v", clause, args)
	}

	args = nil
	clause = accountSearchWhere("+86 138 0000 0040", &args)
	if !strings.Contains(clause, "phone = $1") || len(args) != 1 || args[0] != "13800000040" {
		t.Fatalf("normalized phone clause = %q args=%v", clause, args)
	}

	args = nil
	clause = accountSearchWhere("Alpha.Owner@Example.com", &args)
	if !strings.Contains(clause, "= $1") || len(args) != 1 || args[0] != "alpha.owner@example.com" {
		t.Fatalf("complete email clause = %q args=%v", clause, args)
	}
	if strings.Contains(clause, "position") {
		t.Fatalf("complete email must not fall back to substring matching: %s", clause)
	}
}

func TestCompletePhoneQuery(t *testing.T) {
	cases := []struct {
		term     string
		want     string
		complete bool
	}{
		{"13800138000", "13800138000", true},
		{"+86 138-0013-8000", "13800138000", true},
		{"8613800138000", "13800138000", true},
		{"1380013800", "", false},
		{"138001380000", "", false},
		{"", "", false},
	}
	for _, tc := range cases {
		got, ok := completePhoneQuery(tc.term)
		if got != tc.want || ok != tc.complete {
			t.Fatalf("completePhoneQuery(%q) = (%q,%v), want (%q,%v)", tc.term, got, ok, tc.want, tc.complete)
		}
	}
}

func TestIsCompleteEmailQuery(t *testing.T) {
	cases := []struct {
		term string
		ok   bool
	}{
		{"alpha.owner@example.com", true},
		{"ops@intranet", true},
		{"alpha.owner", false},
		{"a@@b", false},
		{"@example.com", false},
		{"alpha@", false},
		{"alpha owner@example.com", false},
		{"a***@example.com", false}, // 可见掩码走掩码分支，不是完整邮箱
		{"", false},
	}
	for _, tc := range cases {
		if got := isCompleteEmailQuery(tc.term); got != tc.ok {
			t.Fatalf("isCompleteEmailQuery(%q) = %v, want %v", tc.term, got, tc.ok)
		}
	}
}

func TestEmailMaskParts(t *testing.T) {
	cases := []struct {
		term   string
		prefix string
		suffix string
		ok     bool
	}{
		{"a***@example.com", "a", "@example.com", true},
		{"***@example.com", "", "@example.com", true},
		{"a***", "a", "", true},
		{"***", "", "", false},
		{"a***b***c", "", "", false},
		{"plain", "", "", false},
	}
	for _, tc := range cases {
		prefix, suffix, ok := emailMaskParts(tc.term)
		if ok != tc.ok || prefix != tc.prefix || suffix != tc.suffix {
			t.Fatalf("emailMaskParts(%q) = (%q, %q, %v), want (%q, %q, %v)",
				tc.term, prefix, suffix, ok, tc.prefix, tc.suffix, tc.ok)
		}
	}
}
