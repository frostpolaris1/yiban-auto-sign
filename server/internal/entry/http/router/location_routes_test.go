package router_test

// 定位端点未装配时的接线门。
//
// 断言 Locations 为 nil 时 GET/PUT /api/accounts/{id}/location 仍命中真实路由，
// 回 501 与冻结的 internal_error 信封。
// 路由静默消失会变成 404 或 405，拆包漏接会绕过 notImplemented 分支，二者都红。
// 本文件不连数据库，也不替代既有装配用例。

import (
	"net/http"
	"testing"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

func TestLocationRoutesNilHandlerReturn501(t *testing.T) {
	engine, issuer := newTestEngine(t)
	user := issueUser(t, issuer, auth.RoleUser)

	cases := []struct {
		name   string
		method string
		body   string
		csrf   bool
	}{
		{"get", http.MethodGet, "", false},
		{"put", http.MethodPut, `{"mode":"auto"}`, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			csrf := ""
			if tc.csrf {
				csrf = user.CSRFToken
			}
			rec := serve(engine, requestWithSession(
				tc.method, "/api/accounts/a1/location", tc.body, csrf, &user))
			if rec.Code != http.StatusNotImplemented {
				t.Fatalf("%s = %d, want 501; body=%s", tc.method, rec.Code, rec.Body.String())
			}
			if env := decodeEnvelope(t, rec); env.Error.Code != "internal_error" {
				t.Fatalf("code = %q, want internal_error; body=%s", env.Error.Code, rec.Body.String())
			}
		})
	}
}
