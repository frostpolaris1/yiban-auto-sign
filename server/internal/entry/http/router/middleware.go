package router

import (
	"crypto/rand"
	"encoding/hex"
	"log/slog"
	"strconv"
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

var fallbackRequestID atomic.Uint64

// requestID accepts a sane client trace id or mints one. It writes the value
// to the request header first so respond.Error can read it.
func requestID() gin.HandlerFunc {
	return func(c *gin.Context) {
		id := sanitizeRequestID(c.GetHeader(respond.RequestIDHeader))
		if id == "" {
			id = newRequestID()
		}
		c.Request.Header.Set(respond.RequestIDHeader, id)
		c.Header(respond.RequestIDHeader, id)
		c.Set(contextRequestID, id)
		c.Next()
	}
}

func sanitizeRequestID(raw string) string {
	if raw == "" || len(raw) > 64 {
		return ""
	}
	for _, r := range raw {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9':
		case r == '-', r == '_', r == '.':
		default:
			return ""
		}
	}
	return raw
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
// query string, request body or header values.
func accessLog(logger *slog.Logger, clk clock.Clock) gin.HandlerFunc {
	return func(c *gin.Context) {
		start := clk.Now()
		c.Next()
		logger.Info("http request",
			"method", c.Request.Method,
			"path", c.Request.URL.Path,
			"status", c.Writer.Status(),
			"bytes", c.Writer.Size(),
			"duration_ms", clk.Now().Sub(start).Milliseconds(),
			"request_id", c.GetString(contextRequestID),
		)
	}
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
		if token, err := c.Cookie(auth.CookieName); err == nil {
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
