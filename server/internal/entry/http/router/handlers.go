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

// AuthLogin is a P1 placeholder.
func (s *server) AuthLogin(c *gin.Context) { notImplemented(c) }

// AuthLogout is a P1 placeholder. The CSRF gate already ran.
func (s *server) AuthLogout(c *gin.Context, _ oapi.AuthLogoutParams) { notImplemented(c) }

// AuthForgotPassword is a P1 placeholder.
func (s *server) AuthForgotPassword(c *gin.Context) { notImplemented(c) }

// AuthResetPassword is a P1 placeholder.
func (s *server) AuthResetPassword(c *gin.Context) { notImplemented(c) }

// UserCreate is a P1 placeholder.
func (s *server) UserCreate(c *gin.Context, _ oapi.UserCreateParams) { notImplemented(c) }

// SchoolList is a P1 placeholder.
func (s *server) SchoolList(c *gin.Context, params oapi.SchoolListParams) { notImplemented(c) }

// SchoolCreate is a P1 placeholder.
func (s *server) SchoolCreate(c *gin.Context, _ oapi.SchoolCreateParams) { notImplemented(c) }

// SchoolUpdate is a P1 placeholder.
func (s *server) SchoolUpdate(c *gin.Context, id oapi.SchoolID, _ oapi.SchoolUpdateParams) {
	notImplemented(c)
}

// SchoolAdminList is a P1 placeholder.
func (s *server) SchoolAdminList(c *gin.Context, id oapi.SchoolID, params oapi.SchoolAdminListParams) {
	notImplemented(c)
}

// SchoolAdminAdd is a P1 placeholder.
func (s *server) SchoolAdminAdd(c *gin.Context, id oapi.SchoolID, _ oapi.SchoolAdminAddParams) {
	notImplemented(c)
}

// SchoolAdminRemove is a P1 placeholder.
func (s *server) SchoolAdminRemove(c *gin.Context, id oapi.SchoolID, uid oapi.UserID, _ oapi.SchoolAdminRemoveParams) {
	notImplemented(c)
}

// AccountList is a P1 placeholder.
func (s *server) AccountList(c *gin.Context, params oapi.AccountListParams) { notImplemented(c) }

// AccountCreate is a P1 placeholder.
func (s *server) AccountCreate(c *gin.Context, _ oapi.AccountCreateParams) { notImplemented(c) }

// AccountGet is a P1 placeholder.
func (s *server) AccountGet(c *gin.Context, id oapi.AccountID) { notImplemented(c) }

// AccountUpdate is a P1 placeholder.
func (s *server) AccountUpdate(c *gin.Context, id oapi.AccountID, _ oapi.AccountUpdateParams) {
	notImplemented(c)
}

// AccountDelete is a P1 placeholder.
func (s *server) AccountDelete(c *gin.Context, id oapi.AccountID, _ oapi.AccountDeleteParams) {
	notImplemented(c)
}

// AccountReview is a P1 placeholder.
func (s *server) AccountReview(c *gin.Context, id oapi.AccountID, _ oapi.AccountReviewParams) {
	notImplemented(c)
}

// AccountPause is a P1 placeholder.
func (s *server) AccountPause(c *gin.Context, id oapi.AccountID, _ oapi.AccountPauseParams) {
	notImplemented(c)
}
