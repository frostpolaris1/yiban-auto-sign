package assembly

import (
	"net/http"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
)

// TestServerWriteTimeout 钉住写超时的用户可见语义。
//
// WriteTimeout 从读到请求头起算，到点切断本次连接的一切写入。
// SSE 长流靠清本次连接的截止时间绕过它（events/sse.go）；
// 其余响应直接受它约束。改这个值会改变慢客户端的行为，必须显式确认。
// 数值在代码里只有一个定义点（httpWriteTimeout）；本测试是独立校验者。
func TestServerWriteTimeout(t *testing.T) {
	srv := newServerFromStack(
		&config.Config{HTTPAddr: "127.0.0.1:0"},
		&Stack{Engine: http.NewServeMux(), Close: func() {}},
		false,
	)
	if srv.WriteTimeout != 30*time.Second {
		t.Fatalf("WriteTimeout = %v, want 30s", srv.WriteTimeout)
	}
}
