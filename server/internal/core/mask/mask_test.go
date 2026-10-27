package mask

import (
	"regexp"
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

// 时间戳的「秒.纳秒」会拼出 11 位数字：检测口径不得判为手机号。
// 这是 P3-D 的 pii.absent 假阳性根因（SignedAt 的 RFC3339Nano）。
func TestTextHasPhoneIgnoresTimestampFractions(t *testing.T) {
	bad := []string{
		"2026-10-26T13:10:16.429881512+08:00",
		"2026-10-26 13:10:16.429881512",
		"2026-10-26T13:10:16.123456789+08:00",
	}
	for _, s := range bad {
		if TextHasPhone(s) {
			t.Fatalf("TextHasPhone(%q) = true; 时间戳不得判为手机号", s)
		}
	}
	good := []string{
		"139-0013-9000",
		"1 3 8 0 0 1 3 8 0 0 0",
		"139.0013.9000",
		"+86 139 0013 9000",
	}
	for _, s := range good {
		if !TextHasPhone(s) {
			t.Fatalf("TextHasPhone(%q) = false; 分隔手机号必须被抓", s)
		}
	}
}

// 宽分组不判为手机号：时间戳的秒.纳秒是 2+9 段。
// 5 位以上的异常分组属已登记限制；掩码口径仍会处理。
func TestTextHasPhoneRejectsWideSegments(t *testing.T) {
	for _, s := range []string{"13800 138000", "138-00138000", "1380013800 0"} {
		if TextHasPhone(s) {
			t.Fatalf("TextHasPhone(%q) = true; 宽分组不得判为手机号", s)
		}
	}
	if got := MaskText("13800 138000"); got != "138****8000" {
		t.Fatalf("MaskText = %q; 掩码口径必须仍处理", got)
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
		"13900139000",
		"1é23456789",
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

// 检测口径必须覆盖常见地址形状；已登记不覆盖的形状见 emailInText 注释。
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

// R4A：段内连写两个手机号必须全部掩掉。
// 只掩最左会给审计链留下一个明文号码（审计底稿 P2）。
func TestMaskTextMasksEveryFusedPhone(t *testing.T) {
	const raw = "1380013800013800138001"
	got := MaskText(raw)
	if want := "138****8000138****8001"; got != want {
		t.Fatalf("MaskText(%q) = %q, want %q", raw, got, want)
	}
	if TextHasPhone(got) {
		t.Fatalf("掩码后仍检测到手机号: %q", got)
	}
	if phoneShapePattern.MatchString(got) {
		t.Fatalf("掩码后仍残留完整 11 位号码: %q", got)
	}
	if twice := MaskText(got); twice != got {
		t.Fatalf("幂等失败: %q -> %q", got, twice)
	}
}

// 检测口径必须看出段内连写的号码：审计守卫据此拒绝原文。
func TestTextHasPhoneDetectsFusedPhones(t *testing.T) {
	if !TextHasPhone("1380013800013800138001") {
		t.Fatal("22 位连写号码必须被抓")
	}
}

// 11 至 21 位整段保持对齐口径：秒/毫秒/微秒/纳秒时间戳不得被冤成号码。
// 12 至 21 位段内嵌的号码不判，属已登记限制；掩码仍会改写它。
func TestTextHasPhoneLeavesTimestampsAlone(t *testing.T) {
	for _, s := range []string{
		"1735689600",            // 秒
		"1735689600000",         // 毫秒
		"1735689600000000",      // 微秒
		"1735689600000000000",   // 纳秒
		"173568960000000000001", // 21 位：对齐口径上界
		"13900139000100",        // 14 位：嵌入号码不判（已登记）
		"138001380001",          // 12 位：同上
	} {
		if TextHasPhone(s) {
			t.Fatalf("TextHasPhone(%q) = true; 时间戳/短整段不得被判为号码", s)
		}
	}
}

// phoneShapePattern 是「完整 11 位手机号」的检测形状，只用于断言残留。
var phoneShapePattern = regexp.MustCompile(`1[3-9][0-9]{9}`)
