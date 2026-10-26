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
