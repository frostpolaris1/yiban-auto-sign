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
