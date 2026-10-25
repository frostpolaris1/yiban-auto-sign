package user

import "testing"

// 邮箱口径的边界：任何 Unicode 空白都拒绝；ASCII 正常地址通过。
func TestValidEmailRejectsUnicodeSpace(t *testing.T) {
	bad := []string{
		"a\vb@example.com",
		"a\u00a0b@example.com",
		"a*b@example.com",
		"a@@example.com",
		"@example.com",
		"a@",
		"ab",
	}
	for _, email := range bad {
		if ValidEmail(NormalizeEmail(email)) {
			t.Fatalf("ValidEmail(%q) = true; want false", email)
		}
	}
	good := []string{"a@b", "user@example.com", "学生@example.com"}
	for _, email := range good {
		if !ValidEmail(NormalizeEmail(email)) {
			t.Fatalf("ValidEmail(%q) = false; want true", email)
		}
	}
	// 首尾空白由归一化负责：trim 后合法即通过。
	if NormalizeEmail("  A@Example.COM  ") != "a@example.com" {
		t.Fatalf("NormalizeEmail = %q", NormalizeEmail("  A@Example.COM  "))
	}
	if !ValidEmail(NormalizeEmail("  a@example.com  ")) {
		t.Fatal("首尾空白应在归一化时去掉")
	}
}

// 零宽与双向控制等格式字符（Unicode Cf）一律拒绝：
// 两个渲染相同的地址会造成仿冒面（N4）。
func TestValidEmailRejectsFormatCharacters(t *testing.T) {
	bad := []string{
		"zero\u200bwidth@example.com",
		"zero\ufeffwidth@example.com",
		"left\u200etoright@example.com",
		"word\u2060joiner@example.com",
	}
	for _, email := range bad {
		if ValidEmail(NormalizeEmail(email)) {
			t.Fatalf("ValidEmail(%q) = true; want false", email)
		}
	}
	if !ValidEmail(NormalizeEmail("学生@example.com")) {
		t.Fatal("普通非 ASCII 邮箱必须继续通过")
	}
}
