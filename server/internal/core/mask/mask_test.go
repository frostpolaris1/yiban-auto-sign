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
