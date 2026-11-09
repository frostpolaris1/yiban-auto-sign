package webhook_test

// core/webhook 的失败分类判据：DeliveryError 的 Error() 只含分类，
// 底层错误经 Unwrap 可达；Classify 对未知错误保守判不可重试。

import (
	"errors"
	"fmt"
	"net"
	"strings"
	"testing"

	corewebhook "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/webhook"
)

// Error() 只含分类与可重试标志：SendKey 拼在 URL path、URL 本身是凭据、
// 状态码是端口探测指纹，都不得经错误串外泄。
func TestDeliveryErrorStringHoldsOnlyClass(t *testing.T) {
	const secret = "SCT_FAKE_TEST_KEY_000000"
	err := &corewebhook.DeliveryError{
		Kind:      corewebhook.KindRefused,
		Retryable: false,
		Err:       errors.New("POST https://sctapi.ftqq.com/" + secret + ".send: 400 Bad Request"),
	}
	got := err.Error()
	for _, leaked := range []string{secret, "sctapi", "400", "Bad Request"} {
		if strings.Contains(got, leaked) {
			t.Fatalf("Error() 泄漏细节 %q: %s", leaked, got)
		}
	}
	if !strings.Contains(got, "kind=refused") || !strings.Contains(got, "retryable=false") {
		t.Fatalf("Error() 缺分类: %s", got)
	}
	// Unwrap 保留底层错误供 errors.Is/As：日志面不输出，判断面不丢链。
	if !errors.Is(err, err.Unwrap()) {
		t.Fatal("Unwrap 必须保留底层错误")
	}
}

// Classify：nil 空、DeliveryError 透传、未知错误不可重试。
func TestClassify(t *testing.T) {
	if kind, retryable := corewebhook.Classify(nil); kind != "" || retryable {
		t.Fatalf("nil → (%q, %t)", kind, retryable)
	}
	kind, retryable := corewebhook.Classify(&corewebhook.DeliveryError{
		Kind: corewebhook.KindBadResponse, Retryable: true,
	})
	if kind != corewebhook.KindBadResponse || !retryable {
		t.Fatalf("DeliveryError → (%q, %t)", kind, retryable)
	}
	// 未知错误不得被当成可重试：实现缺陷或未知传输态宁可停轮排查。
	kind, retryable = corewebhook.Classify(errors.New("mystery"))
	if kind != corewebhook.KindRefused || retryable {
		t.Fatalf("unknown → (%q, %t), want refused/unretryable", kind, retryable)
	}
	// 超时经 Unwrap 可达：调用方可用 errors.As 判取消。
	wrapped := &corewebhook.DeliveryError{
		Kind: corewebhook.KindTimeout, Retryable: true,
		Err: fmt.Errorf("dial: %w", &net.DNSError{Err: "i/o timeout", Name: "example.test"}),
	}
	var dnsErr *net.DNSError
	if !errors.As(wrapped, &dnsErr) {
		t.Fatal("Unwrap 链上的底层错误必须可达")
	}
}
