package main

// 口令策略读取端点的 e2e。
//
// # 功能
// 覆盖三种会话角色的档位与未登录 401，走真库、真登录、真 cookie、真 HTTP。
// 本文件**不**测性能，**不**改口令。
//
// # 归属
// cmd/server 是唯一可同时引用 adapters、app、entry 的位置。
// 环境复用 auth_e2e_test.go 的 newAuthEnv。
//
// # 口径
// 缺 DSN 时整包跳过，并打印横幅。跳过不是绿。
// 每个测试独占一个 schema；邮箱一律用保留域 example.com。

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

// ppPolicyBody 是响应的契约形状：只有三个数字。
type ppPolicyBody struct {
	MinChars   int `json:"min_chars"`
	MinClasses int `json:"min_classes"`
	MaxBytes   int `json:"max_bytes"`
}

// TestPasswordPolicyByRole 钉住 user 10/2、admin 与 master 12/3，字节上限 1024。
func TestPasswordPolicyByRole(t *testing.T) {
	e := newAuthEnv(t)

	// 未登录：真 HTTP 401。
	resp, payload := e.do(http.MethodGet, "/api/auth/password-policy", nil, nil)
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("unauthenticated = %d, want 401; body=%q", resp.StatusCode, payload)
	}
	if code, _ := errorCode(t, payload); code != "unauthorized" {
		t.Fatalf("error code = %q, want unauthorized", code)
	}

	cases := []struct {
		name       string
		email      string
		role       coreauth.Role
		minChars   int
		minClasses int
		maxBytes   int
	}{
		{"user", "pp-user@example.com", coreauth.RoleUser, 10, 2, 1024},
		{"admin", "pp-admin@example.com", coreauth.RoleAdmin, 12, 3, 1024},
		{"master", "pp-master@example.com", coreauth.RoleMaster, 12, 3, 1024},
	}
	const password = "correct-horse-9!"
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ux8CreateUser(t, e, tc.email, password, tc.role)
			client := u1FreshClient(t)
			u1LoginOn(e, client, tc.email, password)

			resp, payload := u1Do(e, client, http.MethodGet, "/api/auth/password-policy", nil, "")
			if resp.StatusCode != http.StatusOK {
				t.Fatalf("status = %d, want 200; body=%q", resp.StatusCode, payload)
			}
			var body ppPolicyBody
			if err := json.Unmarshal(payload, &body); err != nil {
				t.Fatalf("decode: %v; body=%q", err, payload)
			}
			if body.MinChars != tc.minChars || body.MinClasses != tc.minClasses || body.MaxBytes != tc.maxBytes {
				t.Fatalf("body = %+v, want %d/%d/%d", body, tc.minChars, tc.minClasses, tc.maxBytes)
			}
			raw := string(payload)
			for _, banned := range []string{"password must", "characters", "uppercase", "symbols", tc.email} {
				if strings.Contains(raw, banned) {
					t.Fatalf("response leaked %q: %s", banned, raw)
				}
			}
			var fields map[string]json.RawMessage
			if err := json.Unmarshal(payload, &fields); err != nil {
				t.Fatalf("decode fields: %v", err)
			}
			if len(fields) != 3 {
				t.Fatalf("response fields = %v, want exactly min_chars/min_classes/max_bytes", fields)
			}
		})
	}
}
