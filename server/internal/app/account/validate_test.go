package account

// 会进入凭据密文的自由文本的长度上界。
//
// 契约声明 maxLength: 64；本文件钉住用例层的同一口径。
// 本文件**不**连库、**不**测加密。

import (
	"strings"
	"testing"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

func TestCredentialTextLimit(t *testing.T) {
	for name, value := range map[string]string{
		"empty":            "",
		"at-limit":         strings.Repeat("a", 64),
		"unicode-at-limit": strings.Repeat("机", 64),
	} {
		if err := validateCredentialText("device_code", value); err != nil {
			t.Fatalf("%s: %v", name, err)
		}
	}
	for name, value := range map[string]string{
		"over-limit":         strings.Repeat("a", 65),
		"unicode-over-limit": strings.Repeat("机", 65),
	} {
		err := validateCredentialText("device_code", value)
		if err == nil {
			t.Fatalf("%s: value was accepted", name)
		}
		if code := errs.From(err).Code; code != errs.CodeValidation {
			t.Fatalf("%s: code = %q, want validation_failed", name, code)
		}
	}
}
