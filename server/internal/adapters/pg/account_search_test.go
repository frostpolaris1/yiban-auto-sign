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
		"138",              // 1..3 位前缀
		"0041",             // 4 位后缀
		"13800138000",      // 11 位全号
		"123456",           // 5..10 位：不参与号码匹配
		"a***@example.com", // 可见掩码
		"100%",             // LIKE 元字符按字面
		"东区宿舍",             // 中文名称子串
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
	if strings.Contains(clause, "phone") {
		t.Fatalf("5..10 digit fragment produced a phone clause: %s", clause)
	}
	if len(args) != 1 {
		t.Fatalf("args = %d, want only the text term", len(args))
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
