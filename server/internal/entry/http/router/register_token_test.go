package router_test

// 注册令牌长度上限的接线测试。
//
// # 功能
// 钉住处理器对 RegisterRequest.turnstile_token 的 4096 字符上限：
// 超限在转发给验证服务之前就回 400 human_verification_failed；
// 恰好等于上限的令牌照常进入用例层。
// 本文件**不**连数据库、**不**发真实 siteverify 请求。

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// envelope 是 respond 的错误信封外形。
type envelope struct {
	Error struct {
		Code string `json:"code"`
	} `json:"error"`
}

// registerBody 拼一份合法注册请求体，令牌长度由调用方决定。
func registerBody(token string) string {
	return `{"email":"new@example.com","password":"Correct-horse-9!","agree_terms":true,"agree_privacy":true,"terms_version":1,"privacy_version":1,"turnstile_token":"` + token + `"}`
}

// 超限令牌在验证服务之前就被拒绝：400 human_verification_failed。
func TestRegisterRejectsOversizedTurnstileToken(t *testing.T) {
	engine := newAuthEngine(t, newStubIssuer(), stubAuth{})
	for _, tc := range []struct {
		name string
		size int
		code string
	}{
		{name: "at the limit", size: 4096},
		{name: "one byte over", size: 4097, code: "human_verification_failed"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			body := strings.NewReader(registerBody(strings.Repeat("a", tc.size)))
			req := httptest.NewRequest(http.MethodPost, "/api/auth/register", body)
			req.Header.Set("Content-Type", "application/json")
			rec := serve(engine, req)
			if tc.code == "" {
				// 恰好等于上限：允许落进用例层（stubAuth 建不出会话，
				// 但绝不能被判成 human_verification_failed）。
				if rec.Code == http.StatusBadRequest {
					var got envelope
					_ = json.Unmarshal(rec.Body.Bytes(), &got)
					if got.Error.Code == "human_verification_failed" {
						t.Fatalf("token of %d bytes was rejected: %q", tc.size, rec.Body.String())
					}
				}
				return
			}
			if rec.Code != http.StatusBadRequest {
				t.Fatalf("status = %d, want 400, body = %q", rec.Code, rec.Body.String())
			}
			var got envelope
			if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
				t.Fatalf("decode: %v", err)
			}
			if got.Error.Code != tc.code {
				t.Fatalf("code = %q, want %q", got.Error.Code, tc.code)
			}
		})
	}
}
