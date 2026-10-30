package router

// 账号键归一的入口层判据：大小写变体必须共用一桶。
// 校验与签到用路径里的账号 id；UUID 的大小写不敏感，不归一小写就能
// 把同一个账号拆成多个桶，绕过每账号限流。

import (
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"

	ratelimit "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/ratelimit"
)

func TestAccountLimitSharesBucketAcrossCaseVariants(t *testing.T) {
	limiter, err := ratelimit.NewThrottle(ratelimit.Quota{PerMinute: 1, Burst: 1})
	if err != nil {
		t.Fatalf("NewThrottle: %v", err)
	}
	gin.SetMode(gin.TestMode)
	s := &server{rateLimit: &ratelimit.Guard{Account: limiter}}

	allow := func(key string) bool {
		c, _ := gin.CreateTestContext(httptest.NewRecorder())
		c.Request = httptest.NewRequest("POST", "/api/accounts/x/sign", nil)
		return s.accountLimit(c, key)
	}
	if !allow("0190ABCD-0000-7000-8000-000000000001") {
		t.Fatal("首次必须放行")
	}
	if allow("0190abcd-0000-7000-8000-000000000001") {
		t.Fatal("大小写变体必须共用一桶")
	}
	if !allow("0190abcd-0000-7000-8000-000000000002") {
		t.Fatal("不同账号不得互相挤占")
	}
}
