package mail_test

// 端口与失败分类的纯逻辑测试。

import (
	"errors"
	"strings"
	"testing"

	coremail "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mail"
)

// 分类必须原样穿过 DeliveryError，且 Error() 不含底层文本。
func TestClassify(t *testing.T) {
	inner := errors.New("535 5.7.8 authentication failed for user@example.test")
	err := &coremail.DeliveryError{Kind: coremail.KindAuth, Retryable: false, Err: inner}
	kind, retryable := coremail.Classify(err)
	if kind != coremail.KindAuth || retryable {
		t.Fatalf("Classify = (%s, %t), want (auth, false)", kind, retryable)
	}
	if text := err.Error(); len(text) == 0 || strings.Contains(text, "535") || strings.Contains(text, "user@example.test") {
		t.Fatalf("Error() 暴露了底层文本: %q", text)
	}
	if !errors.Is(err, inner) {
		t.Fatal("Unwrap 没有保留原始错误")
	}
}

// 未知错误默认不可重试：错误方向必须保守。
func TestClassifyUnknownIsNotRetryable(t *testing.T) {
	kind, retryable := coremail.Classify(errors.New("boom"))
	if kind != coremail.KindProtocol || retryable {
		t.Fatalf("Classify(unknown) = (%s, %t), want (protocol, false)", kind, retryable)
	}
	if kind, _ := coremail.Classify(nil); kind != "" {
		t.Fatalf("Classify(nil) = %q, want empty", kind)
	}
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
