// Package router assembles the Gin engine, the middleware chain and the
// generated contract handlers. It reads the session cookie and never talks to
// a database: stores are injected through Options.
package router

import (
	"errors"
	"log/slog"
	"net/http"

	"github.com/gin-gonic/gin"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
)

// Options wires the router. Sessions is required; the rest have defaults.
type Options struct {
	Sessions auth.SessionIssuer
	Clock    clock.Clock
	Logger   *slog.Logger
}

// server implements the generated contract interface. This phase answers 501
// for every business endpoint; only /healthz and /api/auth/me are real.
type server struct {
	sessions auth.SessionIssuer
}

// New builds the HTTP engine. It fails closed on a missing SessionIssuer.
func New(opts Options) (*gin.Engine, error) {
	if opts.Sessions == nil {
		return nil, errors.New("router: nil SessionIssuer")
	}
	clk := opts.Clock
	if clk == nil {
		clk = clock.System{}
	}
	logger := opts.Logger
	if logger == nil {
		logger = slog.Default()
	}
	srv := &server{sessions: opts.Sessions}

	engine := gin.New()
	engine.Use(
		requestID(),
		recoverPanic(logger),
		accessLog(logger, clk),
		srv.authenticate(),
	)
	oapi.RegisterHandlersWithOptions(engine, srv, oapi.GinServerOptions{
		ErrorHandler: func(c *gin.Context, _ error, _ int) {
			respond.Error(c.Writer, c.Request, errs.New(errs.CodeValidation, "invalid request"))
		},
	})
	engine.NoRoute(func(c *gin.Context) {
		respond.Error(c.Writer, c.Request, errs.New(errs.CodeNotFound, "route not found"))
	})
	return engine, nil
}

// GetCurrentUser returns the session principal.
func (s *server) AuthMe(c *gin.Context) {
	sess, ok := auth.FromContext(c.Request.Context())
	if !ok {
		respond.Error(c.Writer, c.Request, errs.New(errs.CodeUnauthorized, "authentication required"))
		return
	}
	respond.JSON(c.Writer, http.StatusOK, oapi.SessionUser{
		Id:                 sess.User.ID,
		Email:              openapiEmail(sess.User.Email),
		Role:               oapi.UserRole(sess.User.Role),
		Status:             oapi.UserStatus(sess.User.Status),
		MustChangePassword: sess.User.MustChangePassword,
		CsrfToken:          sess.CSRFToken,
	})
}

// Healthz reports that the process is serving requests.
func (s *server) HealthCheck(c *gin.Context) {
	respond.JSON(c.Writer, http.StatusOK, oapi.HealthStatus{Status: oapi.HealthStatusStatus("ok")})
}
