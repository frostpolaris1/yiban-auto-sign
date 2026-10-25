package router

// 会话 cookie 的单元测试。
//
// # 功能
// 钉住生产与开发两套 cookie 名与属性。
// 本文件**不**启动 HTTP 服务。
//
// # 口径
// 生产必须 __Host- 前缀 + Secure + HttpOnly + SameSite=Lax + Path=/。
// 开发去掉前缀与 Secure，其余属性不变。

import (
	"net/http"
	"net/http/httptest"
	"testing"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

func parseSetCookie(t *testing.T, rec *httptest.ResponseRecorder) *http.Cookie {
	t.Helper()
	cookies := rec.Result().Cookies()
	if len(cookies) != 1 {
		t.Fatalf("Set-Cookie count = %d, want 1", len(cookies))
	}
	return cookies[0]
}

func TestProductionCookieAttributes(t *testing.T) {
	rec := httptest.NewRecorder()
	newSessionCookie(false).set(rec, "token-value")
	cookie := parseSetCookie(t, rec)

	if cookie.Name != coreauth.CookieName {
		t.Fatalf("cookie name = %q, want %q", cookie.Name, coreauth.CookieName)
	}
	if cookie.Value != "token-value" {
		t.Fatalf("cookie value = %q", cookie.Value)
	}
	if !cookie.Secure {
		t.Fatal("production cookie must set Secure")
	}
	if !cookie.HttpOnly {
		t.Fatal("cookie must set HttpOnly")
	}
	if cookie.SameSite != http.SameSiteLaxMode {
		t.Fatalf("SameSite = %v, want Lax", cookie.SameSite)
	}
	if cookie.Path != "/" {
		t.Fatalf("Path = %q, want /", cookie.Path)
	}
	// 会话 cookie 不设 Max-Age：窗口由服务端判据执行。
	if cookie.MaxAge != 0 || !cookie.Expires.IsZero() {
		t.Fatalf("cookie carries Max-Age=%d Expires=%s; want a browser-session cookie", cookie.MaxAge, cookie.Expires)
	}
}

func TestDevelopmentCookieAttributes(t *testing.T) {
	rec := httptest.NewRecorder()
	newSessionCookie(true).set(rec, "token-value")
	cookie := parseSetCookie(t, rec)

	if cookie.Name != coreauth.InsecureCookieName {
		t.Fatalf("cookie name = %q, want %q", cookie.Name, coreauth.InsecureCookieName)
	}
	if cookie.Secure {
		t.Fatal("development cookie must not set Secure")
	}
	if !cookie.HttpOnly || cookie.SameSite != http.SameSiteLaxMode || cookie.Path != "/" {
		t.Fatalf("cookie attributes = %+v", cookie)
	}
}

func TestClearCookieExpiresImmediately(t *testing.T) {
	for _, insecure := range []bool{false, true} {
		rec := httptest.NewRecorder()
		newSessionCookie(insecure).clear(rec)
		cookie := parseSetCookie(t, rec)
		if cookie.Value != "" {
			t.Fatalf("cleared cookie value = %q, want empty", cookie.Value)
		}
		if cookie.MaxAge >= 0 {
			t.Fatalf("cleared cookie MaxAge = %d, want negative", cookie.MaxAge)
		}
	}
}

// 清除用的 cookie 必须与设置用的 cookie 除 value 与过期字段外逐属性相同。
// 属性不一致时浏览器可能保留另一条同名 cookie：服务端已登出，
// 浏览器却仍带着旧令牌发请求。
func TestClearCookieMirrorsSetAttributes(t *testing.T) {
	for _, insecure := range []bool{false, true} {
		rec := httptest.NewRecorder()
		c := newSessionCookie(insecure)
		c.set(rec, "token-value")
		c.clear(rec)

		cookies := rec.Result().Cookies()
		if len(cookies) != 2 {
			t.Fatalf("Set-Cookie count = %d, want 2", len(cookies))
		}
		set, clear := cookies[0], cookies[1]

		if set.Domain != "" || clear.Domain != "" {
			t.Fatalf("domains = %q and %q, want empty", set.Domain, clear.Domain)
		}
		if clear.Name != set.Name {
			t.Fatalf("clear name = %q, set name = %q", clear.Name, set.Name)
		}
		if clear.Path != set.Path {
			t.Fatalf("clear path = %q, set path = %q", clear.Path, set.Path)
		}
		if clear.Secure != set.Secure {
			t.Fatalf("clear Secure = %v, set Secure = %v", clear.Secure, set.Secure)
		}
		if clear.HttpOnly != set.HttpOnly {
			t.Fatalf("clear HttpOnly = %v, set HttpOnly = %v", clear.HttpOnly, set.HttpOnly)
		}
		if clear.SameSite != set.SameSite {
			t.Fatalf("clear SameSite = %v, set SameSite = %v", clear.SameSite, set.SameSite)
		}
		if clear.Value != "" {
			t.Fatalf("clear value = %q, want empty", clear.Value)
		}
		if clear.MaxAge >= 0 {
			t.Fatalf("clear MaxAge = %d, want negative", clear.MaxAge)
		}
	}
}
