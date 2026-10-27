package protocol

import (
	"strings"
	"testing"
)

func TestExtractVerifyRequestFixture(t *testing.T) {
	token, ok := ExtractVerifyRequest(string(readFixture(t, "iframe_location.txt")))
	if !ok {
		t.Fatal("verify_request was not extracted")
	}
	if len(token) != 512 {
		t.Fatalf("token length = %d, want 512", len(token))
	}
	// 只判形状：实拍值已按 AGENTS §十六 换成等长合成串，
	// 钉住实拍前缀等于把令牌片段写回仓库。
	if strings.Trim(token, "0123456789abcdef") != "" {
		t.Fatalf("token 不是小写十六进制：%q", token[:16])
	}
}

func TestExtractVerifyRequest(t *testing.T) {
	cases := []struct {
		in   string
		want string
		ok   bool
	}{
		{"https://c.uyiban.com/#/?verify_request=abc", "abc", true},
		{"https://c.uyiban.com/#/?verify_request=abc&yb_uid=1", "abc", true},
		{"https://c.uyiban.com/#/?yb_uid=1", "", false},
		{"", "", false},
	}
	for _, tc := range cases {
		got, ok := ExtractVerifyRequest(tc.in)
		if got != tc.want || ok != tc.ok {
			t.Fatalf("ExtractVerifyRequest(%q) = (%q, %t), want (%q, %t)", tc.in, got, ok, tc.want, tc.ok)
		}
	}
}
