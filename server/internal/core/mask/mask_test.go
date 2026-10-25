package mask

import (
	"testing"
	"unicode/utf8"
)

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

// 设备号按字符切首尾，不按字节：多字节设备码不得产出非法 UTF-8。
// 形状与 W2D P2 的 MaskEmail 相同（N1）。
func TestMaskDeviceIsRuneSafe(t *testing.T) {
	cases := map[string]string{
		"设备码ABCD": "设备****CD",
		"AB😀CD😀":  "AB****D😀",
		"设备码":     "****",
	}
	for in, want := range cases {
		got := MaskDevice(in)
		if got != want {
			t.Fatalf("MaskDevice(%q) = %q, want %q", in, got, want)
		}
		if !utf8.ValidString(got) {
			t.Fatalf("MaskDevice(%q) = %q is not valid UTF-8", in, got)
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

// 检测口径（TextHasPhone）与掩码口径（MaskText）的双向差异钉：
// 掩码宁多勿漏（时间戳改写），检测宁缺勿冤（时间戳不报）。
func TestTextHasPhoneStrictCaliber(t *testing.T) {
	cases := []struct {
		in   string
		want bool
	}{
		{"13900139000", true},
		{"user13800138000", true},
		{"13800138000abc", true},
		{"139-0013-9000", true},
		{"+86 139 0013 9000", true},
		{"{\"note\":\"139-0013-9000\"}", true},
		{"1735689600000", false},
		{"订单 1735689600000 已核对", false},
		{"2026-10-25", false},
		{"0013900139000", false},
	}
	for _, tc := range cases {
		if got := TextHasPhone(tc.in); got != tc.want {
			t.Fatalf("TextHasPhone(%q) = %t; want %t", tc.in, got, tc.want)
		}
	}
}

// 包口径的补充：检测口径放过时间戳与编号，只抓 1[3-9] 形态。
func TestTextHasPhoneIgnoresNonMobileNumbers(t *testing.T) {
	for _, s := range []string{
		"订单 1735689600000 已核对",
		"2026-10-25",
		"0013900139000",
	} {
		if TextHasPhone(s) {
			t.Fatalf("TextHasPhone(%q) = true; 时间戳与编号不得判为手机号", s)
		}
	}
	if !TextHasEmail("a@b.com") || TextHasEmail("a***@b.com") {
		t.Fatal("邮箱检测口径不正确")
	}
}

// 邮箱首字符按字符取，不按字节：多字节本地部不得产出非法 UTF-8。
func TestMaskEmailIsRuneSafe(t *testing.T) {
	if got := MaskEmail("学生@example.com"); got != "学***@example.com" {
		t.Fatalf("MaskEmail = %q", got)
	}
	for _, in := range []string{"学生@example.com", "é@example.com", "a@example.com"} {
		if got := MaskEmail(in); !utf8.ValidString(got) {
			t.Fatalf("MaskEmail(%q) = %q is not valid UTF-8", in, got)
		}
	}
}

// 合法 UTF-8 输入经任何掩码函数后仍是合法 UTF-8。
// 首尾落在多字节字符中间是历史缺陷的形状（W2D P2、N1）。
func TestMasksPreserveValidUTF8(t *testing.T) {
	inputs := []string{
		"学生@example.com",
		"联系人 学生@example.com",
		"设备码ABCD",
		"😀A😀B😀",
		"é@例子.中国",
		"13900139000 学生@example.com",
	}
	funcs := map[string]func(string) string{
		"MaskPhone":  MaskPhone,
		"MaskEmail":  MaskEmail,
		"MaskDevice": MaskDevice,
		"MaskText":   MaskText,
		"EmailLabel": EmailLabel,
	}
	for name, fn := range funcs {
		for _, in := range inputs {
			if got := fn(in); !utf8.ValidString(got) {
				t.Fatalf("%s(%q) = %q is not valid UTF-8", name, in, got)
			}
		}
	}
}

// 检测口径必须覆盖系统接受的地址形状：本地部与域名可含非 ASCII 字母。
// 掩码口径同源，否则审计守卫对这类邮箱失明（N5）。
func TestTextHasEmailCoversNonASCII(t *testing.T) {
	yes := []string{
		"学生@example.com",
		"联系人 学生@example.com",
		"a@例子.com",
		"é@例子.中国",
	}
	for _, in := range yes {
		if !TextHasEmail(in) {
			t.Fatalf("TextHasEmail(%q) = false; want true", in)
		}
	}
	if TextHasEmail("学***@example.com") {
		t.Fatal("已掩码的邮箱不得再判为未掩码")
	}
	if got, want := MaskText("联系人 学生@example.com"), "联系人 学***@example.com"; got != want {
		t.Fatalf("MaskText = %q, want %q", got, want)
	}
	for _, in := range yes {
		once := MaskText(in)
		if twice := MaskText(once); twice != once {
			t.Fatalf("MaskText is not idempotent: %q -> %q", once, twice)
		}
		if TextHasEmail(once) {
			t.Fatalf("MaskText(%q) = %q still detected as an email", in, once)
		}
	}
}
