package router

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"errors"
	"log/slog"
	"math"
	"net/http"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
)

const contextRequestID = "request_id"

// maxRequestBodyBytes 是单请求体的上界：1 MiB。
// 登录、忘记与重置是公开 POST；无上界时绑定会把整个请求体读进内存。
// 合法认证请求体远小于 1 MiB，故正常调用不受影响。
const maxRequestBodyBytes int64 = 1 << 20

// maxPhotoUploadBytes 是图片上传的上界：10 MiB 原图加 multipart 头。
// 只在 POST /api/me/photos 生效；其余路径仍是 1 MiB。
const maxPhotoUploadBytes int64 = 10<<20 + 1<<20

// requestBodyLimit 按路径返回请求体上界。
// 端口与路径的比较用请求行原值，不解析、不重写。
func requestBodyLimit(c *gin.Context) int64 {
	if c.Request.Method == http.MethodPost && c.Request.URL.Path == "/api/me/photos" {
		return maxPhotoUploadBytes
	}
	return maxRequestBodyBytes
}

// publicPaths 永不要求会话。其余 /api 路径一律要求。
var publicPaths = map[string]struct{}{
	"/healthz":                  {},
	"/api/auth/login":           {},
	"/api/auth/logout":          {},
	"/api/auth/forgot-password": {},
	"/api/auth/reset-password":  {},
}

// csrfExemptPaths 要么建立会话，要么不携带会话权限。
// **logout 不豁免**：它销毁会话，需要令牌。
var csrfExemptPaths = map[string]struct{}{
	"/healthz":                  {},
	"/api/auth/login":           {},
	"/api/auth/forgot-password": {},
	"/api/auth/reset-password":  {},
}

var fallbackRequestID atomic.Uint64

// requestID 每次都造新 id。
// **客户端自带值不复用**：审计层记录 trace id，
// 攻击者可控的请求头不得决定写进审计的内容。
func requestID() gin.HandlerFunc {
	return func(c *gin.Context) {
		id := newRequestID()
		c.Request.Header.Set(respond.RequestIDHeader, id)
		c.Header(respond.RequestIDHeader, id)
		c.Set(contextRequestID, id)
		c.Next()
	}
}

func newRequestID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "req-" + strconv.FormatUint(fallbackRequestID.Add(1), 10)
	}
	return hex.EncodeToString(b[:])
}

// listRequestHeaders 列出 RFC 9110 定义为列表值的请求头。
// 生成绑定要求每个头恰好一行；多行被它判成 400。
// RFC 9110 §5.3 允许接收方把同名字段行按逗号合并为一值，语义不变。
// 新增列表头必须登记到本表；非列表头（X-CSRF-Token、Last-Event-ID）
// 多行仍是畸形请求，保持生成绑定的 400。
var listRequestHeaders = []string{"If-None-Match"}

// mergeListHeaders 把多行列表头合并成一行。
// 它必须排在生成处理器之前：绑定直接读 c.Request.Header。
func mergeListHeaders() gin.HandlerFunc {
	return func(c *gin.Context) {
		header := c.Request.Header
		for _, name := range listRequestHeaders {
			values := header.Values(name)
			if len(values) < 2 {
				continue
			}
			header.Set(name, strings.Join(values, ", "))
		}
		c.Next()
	}
}

// bodyLimit 把请求体包成有界读取器。它排在 requestID 之后、其余中间件之前。
// 超界读取在处理器绑定时暴露；bindError 把它转成 413。
// 本中间件**不**自己读请求体：读走即无法再次绑定。
func bodyLimit(limitFor func(*gin.Context) int64) gin.HandlerFunc {
	return func(c *gin.Context) {
		if c.Request.Body != nil {
			c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, limitFor(c))
		}
		c.Next()
	}
}

// bindError 是 respond.BindError 的 Gin 适配。
// 口径只在 respond 实现一份：认证与三个业务域共用。
func bindError(c *gin.Context, err error) {
	respond.BindError(c.Writer, c.Request, err)
}

// recoverPanic 把 panic 转成 500 信封。
// panic 值不记日志，因为它可能携带请求数据。
func recoverPanic(logger *slog.Logger) gin.HandlerFunc {
	return gin.CustomRecovery(func(c *gin.Context, _ any) {
		logger.Error("panic recovered",
			"path", c.Request.URL.Path,
			"request_id", c.GetString(contextRequestID),
		)
		respond.Error(c.Writer, c.Request, errs.New(errs.CodeInternal, "internal error"))
		c.Abort()
	})
}

// accessLog 记录方法、路径、状态与耗时。
// 它**从不**记录查询串、请求体或请求头的值。
// 它排在 panic 恢复之前，故 panic 的请求仍产出一行日志。
func accessLog(logger *slog.Logger, clk clock.Clock) gin.HandlerFunc {
	return func(c *gin.Context) {
		start := clk.Now()
		c.Next()
		attrs := []any{
			"method", c.Request.Method,
			"path", c.Request.URL.Path,
			"status", c.Writer.Status(),
			"bytes", c.Writer.Size(),
			"duration_ms", clk.Now().Sub(start).Milliseconds(),
			"request_id", c.GetString(contextRequestID),
		}
		if sess, ok := auth.FromContext(c.Request.Context()); ok {
			attrs = append(attrs, "user_id", sess.User.ID)
		}
		logger.Info("http request", attrs...)
	}
}

// securityHeaders 设置不随响应变化的头。
func securityHeaders() gin.HandlerFunc {
	return func(c *gin.Context) {
		h := c.Writer.Header()
		h.Set("X-Content-Type-Options", "nosniff")
		h.Set("Referrer-Policy", "no-referrer")
		h.Set("X-Frame-Options", "DENY")
		if strings.HasPrefix(c.Request.URL.Path, "/api/") {
			h.Set("Cache-Control", "no-store")
		}
		c.Next()
	}
}

// rateLimitedPaths 是按客户端 IP 限流的端点。
// 模式取 Gin 注册形状（:id），不是契约的 {id}。
var rateLimitedPaths = map[string]struct{}{
	"/api/auth/login":           {},
	"/api/auth/forgot-password": {},
	"/api/auth/reset-password":  {},
	"/api/auth/change-password": {},
	"/api/accounts/:id/verify":  {},
	"/api/accounts/:id/sign":    {},
}

// rateLimitInbound 在解析会话之前按客户端 IP 限流。
// 未认证洪水不得先付一次会话查询或一次请求体绑定。
func (s *server) rateLimitInbound() gin.HandlerFunc {
	return func(c *gin.Context) {
		if _, limited := rateLimitedPaths[c.FullPath()]; !limited {
			c.Next()
			return
		}
		if ok, wait := s.rateLimit.AllowIP(c.Request.RemoteAddr,
			c.Request.Header.Values("X-Forwarded-For")); !ok {
			tooManyRequests(c, wait)
			c.Abort()
			return
		}
		c.Next()
	}
}

// tooManyRequests 写 429 与 Retry-After。
// Retry-After 取秒并向上取整：0 秒会被客户端读成"立即可重试"。
func tooManyRequests(c *gin.Context, wait time.Duration) {
	seconds := int(math.Ceil(wait.Seconds()))
	if seconds < 1 {
		seconds = 1
	}
	c.Header("Retry-After", strconv.Itoa(seconds))
	respond.Error(c.Writer, c.Request, errs.New(errs.CodeRateLimited, "too many requests"))
}

// accountLimit 在付出 KDF 之前按账号键判限流。
// 键由调用方给出：登录用归一邮箱，重置用令牌摘要，改密用会话 userID，
// 校验与签到用账号 id。这里统一小写：UUID 大小写不敏感，
// 不归一就能用大小写变体把同一个账号拆成多个桶。
func (s *server) accountLimit(c *gin.Context, key string) bool {
	key = strings.ToLower(strings.TrimSpace(key))
	if ok, wait := s.rateLimit.AllowAccount(key); !ok {
		tooManyRequests(c, wait)
		return false
	}
	return true
}

// requireCSRF 拒绝「带会话但不带匹配 X-CSRF-Token 头」的改状态请求。
// 令牌由会话令牌派生，**不存储任何值**。
// 登录、重置密码与健康检查豁免：它们要么建立会话，要么不带会话权限。
func (s *server) requireCSRF() gin.HandlerFunc {
	return func(c *gin.Context) {
		if !unsafeMethod(c.Request.Method) {
			c.Next()
			return
		}
		route := c.FullPath()
		if route == "" {
			c.Next()
			return
		}
		if _, exempt := csrfExemptPaths[route]; exempt {
			c.Next()
			return
		}
		sess, ok := auth.FromContext(c.Request.Context())
		if !ok {
			c.Next()
			return
		}
		got := c.GetHeader("X-CSRF-Token")
		want := sess.CSRFToken
		if got == "" || subtle.ConstantTimeCompare([]byte(got), []byte(want)) != 1 {
			respond.Error(c.Writer, c.Request, errs.New(errs.CodeForbidden, "missing or invalid CSRF token"))
			c.Abort()
			return
		}
		c.Next()
	}
}

func unsafeMethod(method string) bool {
	switch method {
	case http.MethodPost, http.MethodPut, http.MethodPatch, http.MethodDelete:
		return true
	}
	return false
}

// authenticate 解析会话 cookie。
// 未知路由落到 NoRoute（404）；公开路由无会话也放行。
// **数据库故障不是未登录**：只有 ErrNoSession 走 401 与公开路径。
func (s *server) authenticate() gin.HandlerFunc {
	return func(c *gin.Context) {
		route := c.FullPath()
		if route == "" {
			c.Next()
			return
		}
		if token, err := c.Cookie(s.cookies.name); err == nil {
			sess, rerr := s.sessions.Resolve(c.Request.Context(), token)
			if rerr == nil {
				c.Request = c.Request.WithContext(auth.WithSession(c.Request.Context(), sess))
				c.Next()
				return
			}
			if !errors.Is(rerr, auth.ErrNoSession) {
				// 存储故障不得表现成「全体用户被登出」。
				// 也不得放行到公开路径：带着坏会话的请求失败关闭。
				respond.Error(c.Writer, c.Request, errs.New(errs.CodeInternal, "session lookup failed"))
				c.Abort()
				return
			}
		}
		if _, ok := publicPaths[route]; ok {
			c.Next()
			return
		}
		respond.Error(c.Writer, c.Request, errs.New(errs.CodeUnauthorized, "authentication required"))
		c.Abort()
	}
}
