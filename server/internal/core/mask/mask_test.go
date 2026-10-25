package mask

import "testing"

func TestMaskPhone(t *testing.T) {
	cases := map[string]string{
		"13800138000": "138****8000",
		"138****8000": "138****8000",
		"12345":       "12345",
		"23800138000": "23800138000",
	}
	for in, want := range cases {
		if got := MaskPhone(in); got != want {
			t.Fatalf("MaskPhone(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestMaskEmail(t *testing.T) {
	cases := map[string]string{
		"alice@example.com": "a***@example.com",
		"a***@example.com":  "a***@example.com",
		"no-at-sign":        "no-at-sign",
	}
	for in, want := range cases {
		if got := MaskEmail(in); got != want {
			t.Fatalf("MaskEmail(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestMaskDevice(t *testing.T) {
	cases := map[string]string{
		"ABCD1234": "AB****34",
		"AB****34": "AB****34",
		"ABCD":     "****",
	}
	for in, want := range cases {
		if got := MaskDevice(in); got != want {
			t.Fatalf("MaskDevice(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestMaskTextIdempotent(t *testing.T) {
	in := "call 13800138000 or alice@example.com"
	once := MaskText(in)
	if once != "call 138****8000 or a***@example.com" {
		t.Fatalf("MaskText = %q", once)
	}
	if twice := MaskText(once); twice != once {
		t.Fatalf("MaskText is not idempotent: %q -> %q", once, twice)
	}
}

// TestMaskTextCoversSeparatedPhones 钉住 W2C-R2-05。
//
// 用户会把号码写成连字符、空格或 tel: 前缀形式。
// 只匹配 11 个连续数字会让这些形式原样进响应；
// 掩码必须先去掉分隔符再判形状。
func TestMaskTextCoversSeparatedPhones(t *testing.T) {
	cases := map[string]string{
		"139-0013-9000":     "139****9000",
		"+86 139 0013 9000": "139****9000",
		"tel:13900139000":   "tel:139****9000",
		"139 0013 9000":     "139****9000",
		"139.0013.9000":     "139****9000",
		"139****9000":       "139****9000",
		"2026-10-25":        "2026-10-25",
		"order 22345678901": "order 22345678901",
	}
	for in, want := range cases {
		got := MaskText(in)
		if got != want {
			t.Fatalf("MaskText(%q) = %q, want %q", in, got, want)
		}
		if twice := MaskText(got); twice != got {
			t.Fatalf("MaskText is not idempotent: %q -> %q", got, twice)
		}
	}
}

// W2C-R3-01：游程正则会把「手机号 + 空格 + 数字」并成一组。
// 四种基线树回 400 的输入，现在必须掩掉号码、保留邻接数字。
func TestMaskTextHandlesPhoneAdjacentToDigits(t *testing.T) {
	cases := []struct{ in, want string }{
		{"13900139000 2026-10-25", "139****9000 2026-10-25"},
		{"2026-10-25 13900139000", "2026-10-25 139****9000"},
		{"退回：13900139000 100", "退回：139****9000 100"},
		{"12345678901 100", "123****8901 100"},
		{"13900139000\n13900139000", "139****9000\n139****9000"},
	}
	for _, tc := range cases {
		if got := MaskText(tc.in); got != tc.want {
			t.Fatalf("MaskText(%q) = %q; want %q", tc.in, got, tc.want)
		}
	}
}

// W2C-R4-01：分隔号码与相邻数字并存时，跨段的手机号窗口必须被掩，
// 原文不得进响应与审计。三审实测的三种 400→漏掩输入全部钉住。
func TestMaskTextHandlesSeparatedPhoneAdjacentToDigits(t *testing.T) {
	cases := []struct{ in, want string }{
		{"139-0013-9000 100", "139****9000 100"},
		{"139 0013 9000 100", "139****9000 100"},
		{"139.0013.9000 100", "139****9000 100"},
		{"+86 139 0013 9000 100", "139****9000 100"},
		{"2026-10-25 13900139000", "2026-10-25 139****9000"},
		{"13900139000 2026-10-25", "139****9000 2026-10-25"},
	}
	for _, tc := range cases {
		if got := MaskText(tc.in); got != tc.want {
			t.Fatalf("MaskText(%q) = %q; want %q", tc.in, got, tc.want)
		}
	}
	// 幂等：掩码过的文本再掩一遍不变。
	for _, tc := range cases {
		once := MaskText(tc.in)
		if twice := MaskText(once); twice != once {
			t.Fatalf("not idempotent: %q -> %q", once, twice)
		}
	}
	// 日期段是 4 位数字，段内遗留窗口（≥11 位）不会碰到它。
	if got := MaskText("2026-10-25"); got != "2026-10-25" {
		t.Fatalf("pure date was rewritten: %q", got)
	}
}

// 连写的 14 位数字里藏着手机号：段内遗留窗口掩最左命中。
func TestMaskTextHandlesFusedPhoneAndDigits(t *testing.T) {
	got := MaskText("13900139000100")
	if got != "139****9000100" {
		t.Fatalf("fused input = %q", got)
	}
}
