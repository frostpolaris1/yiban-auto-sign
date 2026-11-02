package auth_test

// 口令强度策略的单测。
//
// # 功能
// 钉住最小长度、类别下限、非 ASCII 记法与字节上限。
// 本文件**不**哈希口令，**不**连数据库。

import (
	"strings"
	"testing"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

func TestPasswordPolicyAccepts(t *testing.T) {
	good := []string{
		"Correct-horse-9!", // 四类
		"abcdefghij1",      // 小写＋数字
		"ABCDEFGHIJ1",      // 大写＋数字
		"aaaaaaaaaaa!",     // 小写＋符号
		"中文中文中文ab12",       // 中文记符号，另有小写与数字
	}
	for _, password := range good {
		if reason := coreauth.PasswordPolicyError(password); reason != "" {
			t.Fatalf("PasswordPolicyError(%q) = %q, want pass", password, reason)
		}
	}
}

func TestPasswordPolicyRejects(t *testing.T) {
	if reason := coreauth.PasswordPolicyError("short"); !strings.Contains(reason, "10 characters") {
		t.Fatalf("short password reason = %q", reason)
	}
	if reason := coreauth.PasswordPolicyError("abcdefghij"); !strings.Contains(reason, "two of") {
		t.Fatalf("one-class password reason = %q", reason)
	}
	if reason := coreauth.PasswordPolicyError("!!!!!!!!!!"); !strings.Contains(reason, "two of") {
		t.Fatalf("symbol-only password reason = %q", reason)
	}
	if reason := coreauth.PasswordPolicyError(strings.Repeat("a", coreauth.PasswordMaxBytes+1)); reason != "password is too long" {
		t.Fatalf("oversized password reason = %q", reason)
	}
}

func TestPasswordClassCount(t *testing.T) {
	cases := map[string]int{
		"":          0,
		"abc":       1,
		"aA":        2,
		"aA1":       3,
		"aA1!":      4,
		"中文":        1, // 非 ASCII 记符号
		"中文aA1!":    4,
		"aaaaaaAAA": 2,
	}
	for password, want := range cases {
		if got := coreauth.PasswordClassCount(password); got != want {
			t.Fatalf("PasswordClassCount(%q) = %d, want %d", password, got, want)
		}
	}
}
