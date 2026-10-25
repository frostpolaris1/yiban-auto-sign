package router

// 会话 cookie 的写入点。全仓只此一处设置该 cookie。
//
// # 功能
// 按环境选择名称与 Secure 属性，写入与清除会话 cookie。
// 本文件**不**校验会话，**不**决定会话有效期。
//
// # 归属
// entry/http。认证处理器写 cookie 时只用本类型。
//
// # 口径
// 生产名 __Host-yiban_session，带 Secure；
// 开发名 yiban_session_dev，不带 Secure。两者都是 HttpOnly、Lax、Path=/。
// **不设 Max-Age**：空闲窗口由服务端滑动判定，
// 浏览器侧固定过期会把持续活跃的用户提前踢出。

import (
	"net/http"
	"time"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

// sessionCookie 是一套已定型的 cookie 属性。
type sessionCookie struct {
	name   string
	secure bool
}

// newSessionCookie 按部署形态选择名称与 Secure。
//
// insecure 为真时只用于本地 HTTP：浏览器拒绝不带 Secure 的 __Host- cookie。
func newSessionCookie(insecure bool) sessionCookie {
	if insecure {
		return sessionCookie{name: coreauth.InsecureCookieName}
	}
	return sessionCookie{name: coreauth.CookieName, secure: true}
}

// set 写入会话 cookie。
func (c sessionCookie) set(w http.ResponseWriter, token string) {
	http.SetCookie(w, &http.Cookie{
		Name:     c.name,
		Value:    token,
		Path:     "/",
		HttpOnly: true,
		Secure:   c.secure,
		SameSite: http.SameSiteLaxMode,
	})
}

// clear 让浏览器立即丢弃会话 cookie。
// 属性必须与 set 一致，否则浏览器可能保留另一条同名 cookie。
func (c sessionCookie) clear(w http.ResponseWriter) {
	http.SetCookie(w, &http.Cookie{
		Name:     c.name,
		Value:    "",
		Path:     "/",
		HttpOnly: true,
		Secure:   c.secure,
		SameSite: http.SameSiteLaxMode,
		MaxAge:   -1,
		Expires:  time.Unix(1, 0).UTC(),
	})
}
