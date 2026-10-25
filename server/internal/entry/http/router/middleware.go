package router

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"sync/atomic"

	"github.com/gin-gonic/gin"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
)

const contextRequestID = "request_id"

// publicPaths never require a session. Everything else under /api does.
var publicPaths = map[string]struct{}{
	"/healthz":                  {},
	"/api/auth/login":           {},
	"/api/auth/logout":          {},
	"/api/auth/forgot-password": {},
	"/api/auth/reset-password":  {},
}

// csrfExemptPaths either establish the session or carry no session authority.
// Logout is not exempt: it destroys a session and needs the token.
var csrfExemptPaths = map[string]struct{}{
	"/healthz":                  {},
	"/api/auth/login":           {},
	"/api/auth/forgot-password": {},
	"/api/auth/reset-password":  {},
}

var fallbackRequestID atomic.Uint64

// requestID always mints a fresh id. A client-supplied value is not reused:
// the audit layer records trace ids, and an attacker-controlled header must
// not decide what lands there.
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

// recoverPanic turns a panic into the 500 envelope. The panic value is not
// logged because it may carry request data.
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

// accessLog records method, path, status and duration. It never records the
// query string, request body or header values. It runs before the recovery
// middleware so a panicking request still produces one log line.
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

// securityHeaders sets headers that never vary per response.
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

// requireCSRF rejects state-changing requests that carry a session but no
// matching X-CSRF-Token header. The token is derived from the session token,
// so nothing is stored. Login, password reset and health checks are exempt:
// they either establish the session or carry no session authority.
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

// authenticate resolves the session cookie. Unknown routes fall through to
// NoRoute (404); public routes fall through without a session.
func (s *server) authenticate() gin.HandlerFunc {
	return func(c *gin.Context) {
		route := c.FullPath()
		if route == "" {
			c.Next()
			return
		}
		if token, err := c.Cookie(s.cookieName); err == nil {
			sess, rerr := s.sessions.Resolve(c.Request.Context(), token)
			if rerr == nil {
				c.Request = c.Request.WithContext(auth.WithSession(c.Request.Context(), sess))
				c.Next()
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
