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

// 角色分档：user 10/2、master/admin 12/3；未知角色按 user 档。
func TestPasswordPolicyForRole(t *testing.T) {
	t.Run("user tier", func(t *testing.T) {
		// 10 字符两类恰好通过；11 字符一类仍拒。
		if reason := coreauth.PasswordPolicyErrorForRole("abcdefghij1", coreauth.RoleUser); reason != "" {
			t.Fatalf("user-tier 10/2 = %q, want pass", reason)
		}
		if reason := coreauth.PasswordPolicyErrorForRole("abcdefghi", coreauth.RoleUser); !strings.Contains(reason, "10 characters") {
			t.Fatalf("user-tier 9 chars = %q, want 10 characters", reason)
		}
		if reason := coreauth.PasswordPolicyErrorForRole("abcdefghij", coreauth.RoleUser); !strings.Contains(reason, "two of") {
			t.Fatalf("user-tier one class = %q, want two of", reason)
		}
	})
	t.Run("privileged tier", func(t *testing.T) {
		// 11 字符三类也必须拒：长度差一就是差。
		if reason := coreauth.PasswordPolicyErrorForRole("Abcdefghij1", coreauth.RoleAdmin); !strings.Contains(reason, "12 characters") {
			t.Fatalf("admin 11 chars 3 classes = %q, want 12 characters", reason)
		}
		if reason := coreauth.PasswordPolicyErrorForRole("Abcdefghij1!", coreauth.RoleAdmin); reason != "" {
			t.Fatalf("admin 12/3 = %q, want pass", reason)
		}
		if reason := coreauth.PasswordPolicyErrorForRole("abcdefghij12", coreauth.RoleAdmin); !strings.Contains(reason, "three of") {
			t.Fatalf("admin 12 chars 2 classes = %q, want three of", reason)
		}
		if reason := coreauth.PasswordPolicyErrorForRole("Abcdefghij1!", coreauth.RoleMaster); reason != "" {
			t.Fatalf("master 12/3 = %q, want pass", reason)
		}
	})
	t.Run("unknown role falls back to user tier", func(t *testing.T) {
		if reason := coreauth.PasswordPolicyErrorForRole("abcdefghij1", coreauth.Role("robot")); reason != "" {
			t.Fatalf("unknown role 10/2 = %q, want pass", reason)
		}
	})
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
