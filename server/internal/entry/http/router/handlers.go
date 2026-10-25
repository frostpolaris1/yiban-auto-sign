package router

import (
	"github.com/gin-gonic/gin"
	openapi_types "github.com/oapi-codegen/runtime/types"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
)

// openapiEmail converts a stored address to the generated transport type.
func openapiEmail(s string) openapi_types.Email { return openapi_types.Email(s) }

// notImplemented answers 501 with the internal_error envelope. P1 ships the
// contract and the wiring only; each endpoint below is filled by a later task.
func notImplemented(c *gin.Context) { respond.NotImplemented(c.Writer, c.Request) }

// Login is a P1 placeholder.
func (s *server) AuthLogin(c *gin.Context) { notImplemented(c) }

// Logout is a P1 placeholder.
func (s *server) AuthLogout(c *gin.Context) { notImplemented(c) }

// ForgotPassword is a P1 placeholder.
func (s *server) AuthForgotPassword(c *gin.Context) { notImplemented(c) }

// ResetPassword is a P1 placeholder.
func (s *server) AuthResetPassword(c *gin.Context) { notImplemented(c) }

// CreateUser is a P1 placeholder.
func (s *server) UserCreate(c *gin.Context) { notImplemented(c) }

// ListSchools is a P1 placeholder.
func (s *server) SchoolList(c *gin.Context, params oapi.SchoolListParams) { notImplemented(c) }

// CreateSchool is a P1 placeholder.
func (s *server) SchoolCreate(c *gin.Context) { notImplemented(c) }

// UpdateSchool is a P1 placeholder.
func (s *server) SchoolUpdate(c *gin.Context, id oapi.SchoolID) { notImplemented(c) }

// ListSchoolAdmins is a P1 placeholder.
func (s *server) SchoolAdminList(c *gin.Context, id oapi.SchoolID, params oapi.SchoolAdminListParams) {
	notImplemented(c)
}

// AddSchoolAdmin is a P1 placeholder.
func (s *server) SchoolAdminAdd(c *gin.Context, id oapi.SchoolID) { notImplemented(c) }

// RemoveSchoolAdmin is a P1 placeholder.
func (s *server) SchoolAdminRemove(c *gin.Context, id oapi.SchoolID, uid oapi.UserID) {
	notImplemented(c)
}

// ListAccounts is a P1 placeholder.
func (s *server) AccountList(c *gin.Context, params oapi.AccountListParams) { notImplemented(c) }

// CreateAccount is a P1 placeholder.
func (s *server) AccountCreate(c *gin.Context) { notImplemented(c) }

// GetAccount is a P1 placeholder.
func (s *server) AccountGet(c *gin.Context, id oapi.AccountID) { notImplemented(c) }

// UpdateAccount is a P1 placeholder.
func (s *server) AccountUpdate(c *gin.Context, id oapi.AccountID) { notImplemented(c) }

// DeleteAccount is a P1 placeholder.
func (s *server) AccountDelete(c *gin.Context, id oapi.AccountID) { notImplemented(c) }

// ReviewAccount is a P1 placeholder.
func (s *server) AccountReview(c *gin.Context, id oapi.AccountID) { notImplemented(c) }

// PauseAccount is a P1 placeholder.
func (s *server) AccountPause(c *gin.Context, id oapi.AccountID) { notImplemented(c) }
