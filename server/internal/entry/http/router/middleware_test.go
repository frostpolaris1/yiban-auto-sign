package router

import (
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"

	corephoto "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/photo"
)

// 图片上传路径的请求体上界必须容纳 10 MiB 原图加 multipart 头；
// 其余路径仍是 1 MiB，避免公开 POST 被大体积拖住。
func TestRequestBodyLimitPerPath(t *testing.T) {
	if maxPhotoUploadBytes < corephoto.MaxBytes+1<<20 {
		t.Fatalf("上传上限 %d 不足以容纳 %d 字节原图", maxPhotoUploadBytes, corephoto.MaxBytes)
	}
	cases := []struct {
		method, path string
		want         int64
	}{
		{"POST", "/api/me/photos", maxPhotoUploadBytes},
		{"GET", "/api/me/photos", maxRequestBodyBytes},
		{"POST", "/api/auth/login", maxRequestBodyBytes},
		{"PUT", "/api/me/photo-retention", maxRequestBodyBytes},
	}
	for _, tc := range cases {
		ctx, _ := gin.CreateTestContext(httptest.NewRecorder())
		ctx.Request = httptest.NewRequest(tc.method, tc.path, nil)
		if got := requestBodyLimit(ctx); got != tc.want {
			t.Fatalf("%s %s = %d, want %d", tc.method, tc.path, got, tc.want)
		}
	}
}

// 批量签到必须在 IP 限流白名单内：契约对整批声明 429，
// 白名单漏行＝429 运行时不可达。直接断言注册表，漏行即红。
func TestBatchSignPathIsIPRateLimited(t *testing.T) {
	for _, path := range []string{
		"/api/accounts/:id/sign",
		"/api/accounts/batch/sign",
	} {
		if _, ok := rateLimitedPaths[path]; !ok {
			t.Fatalf("%s 不在 rateLimitedPaths 白名单内", path)
		}
	}
	if len(rateLimitedPaths) < 7 {
		t.Fatalf("白名单 = %d 项, want at least 7（表被清空即红）", len(rateLimitedPaths))
	}
}
