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

// 登录、忘记口令、恢复与注册各用独立域：任一域打满不得挤占其余域。
func TestAccountLimitDomainsAreIndependent(t *testing.T) {
	limiter, err := ratelimit.NewThrottle(ratelimit.Quota{PerMinute: 1, Burst: 1})
	if err != nil {
		t.Fatalf("NewThrottle: %v", err)
	}
	gin.SetMode(gin.TestMode)
	s := &server{rateLimit: &ratelimit.Guard{Account: limiter}}

	allow := func(domain, key string) bool {
		c, _ := gin.CreateTestContext(httptest.NewRecorder())
		c.Request = httptest.NewRequest("POST", "/api/auth/login", nil)
		return s.accountLimitFor(c, domain, key)
	}
	const email = "victim@example.com"

	if !allow(accountDomainLogin, email) {
		t.Fatal("登录域首次必须放行")
	}
	if allow(accountDomainLogin, "VICTIM@example.com") {
		t.Fatal("同一域的大小写变体必须共用一桶")
	}
	// 默认域的任意键不得与 auth 域碰撞：账号 id 里写
	// "auth.forgot:<email>" 也不能命中 forgot 桶。
	if !allow("", "auth.forgot:"+email) {
		t.Fatal("默认域首个键必须放行")
	}
	// 登录域已打满：其余三个域各自仍有独立额度。
	if !allow(accountDomainForgot, email) {
		t.Fatal("默认域的任意键挤占了 forgot 域")
	}
	if !allow(accountDomainRestore, email) {
		t.Fatal("登录洪水挤占了 restore 域")
	}
	if !allow(accountDomainRegister, email) {
		t.Fatal("登录洪水挤占了 register 域")
	}
	// 默认域（令牌摘要、用户 id、账号 id）与三个邮箱域互不相干。
	if !allow("", email) {
		t.Fatal("登录洪水挤占了默认账号域")
	}
}

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

// 限流键用 UUID 规范形：urn/花括号形式与规范形共用一桶。
func TestAccountLimitNormalizesUUIDForms(t *testing.T) {
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
	const canonical = "0190abcd-0000-7000-8000-000000000001"
	if !allow(canonical) {
		t.Fatal("规范形首次必须放行")
	}
	if allow("urn:uuid:0190ABCD-0000-7000-8000-000000000001") {
		t.Fatal("urn 形式必须与规范形共用一桶")
	}
}
