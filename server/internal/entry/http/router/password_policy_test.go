package router_test

// 口令策略读取端点的 HTTP 面测试。
//
// # 功能
// 钉住三种角色的档位、未登录 401，以及响应只含数字。
// 本文件**不**连数据库，**不**发真实网络请求。
//
// # 归属
// entry/http/router。会话由 stubIssuer 注入，处理器只读上下文。

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

type passwordPolicyBody struct {
	MinChars   int `json:"min_chars"`
	MinClasses int `json:"min_classes"`
	MaxBytes   int `json:"max_bytes"`
}

// TestPasswordPolicyByRole 覆盖 user 10/2、admin 与 master 12/3，字节上限同为 1024。
func TestPasswordPolicyByRole(t *testing.T) {
	engine, issuer := newTestEngine(t)
	cases := []struct {
		name       string
		role       auth.Role
		minChars   int
		minClasses int
		maxBytes   int
	}{
		{"user", auth.RoleUser, 10, 2, 1024},
		{"admin", auth.RoleAdmin, 12, 3, 1024},
		{"master", auth.RoleMaster, 12, 3, 1024},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			email := "policy-" + string(tc.role) + "@example.com"
			issued, err := issuer.Issue(context.Background(), auth.User{
				ID:     "policy-" + string(tc.role),
				Email:  email,
				Role:   tc.role,
				Status: auth.StatusActive,
			})
			if err != nil {
				t.Fatalf("issue: %v", err)
			}
			req := httptest.NewRequest(http.MethodGet, "/api/auth/password-policy", nil)
			req.AddCookie(&http.Cookie{Name: auth.CookieName, Value: issued.Token})
			rec := serve(engine, req)

			if rec.Code != http.StatusOK {
				t.Fatalf("status = %d, want 200; body=%q", rec.Code, rec.Body.String())
			}
			var body passwordPolicyBody
			if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
				t.Fatalf("decode: %v; body=%q", err, rec.Body.String())
			}
			if body.MinChars != tc.minChars || body.MinClasses != tc.minClasses || body.MaxBytes != tc.maxBytes {
				t.Fatalf("body = %+v, want %d/%d/%d", body, tc.minChars, tc.minClasses, tc.maxBytes)
			}
			// 响应只含数字：没有英文校验文案，也没有邮箱、用户 id 或会话令牌。
			raw := rec.Body.String()
			for _, banned := range []string{
				"password must", "characters", "uppercase", "symbols",
				email, issued.Token, issued.CSRFToken,
			} {
				if strings.Contains(raw, banned) {
					t.Fatalf("response leaked %q: %s", banned, raw)
				}
			}
			var fields map[string]json.RawMessage
			if err := json.Unmarshal(rec.Body.Bytes(), &fields); err != nil {
				t.Fatalf("decode fields: %v", err)
			}
			if len(fields) != 3 {
				t.Fatalf("response fields = %v, want exactly min_chars/min_classes/max_bytes", fields)
			}
		})
	}
}

// TestPasswordPolicyUnauthenticated 钉住无会话 401 与冻结错误信封。
func TestPasswordPolicyUnauthenticated(t *testing.T) {
	engine, _ := newTestEngine(t)
	rec := serve(engine, httptest.NewRequest(http.MethodGet, "/api/auth/password-policy", nil))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401; body=%q", rec.Code, rec.Body.String())
	}
	if env := decodeEnvelope(t, rec); env.Error.Code != "unauthorized" {
		t.Fatalf("code = %q, want unauthorized", env.Error.Code)
	}
}
