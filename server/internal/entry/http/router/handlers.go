package router

import (
	"github.com/gin-gonic/gin"
	openapi_types "github.com/oapi-codegen/runtime/types"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
)

// openapiEmail 把存储地址转成生成的传输类型。
func openapiEmail(s string) openapi_types.Email { return openapi_types.Email(s) }

// notImplemented 以 internal_error 信封回 501。
// P1 只交付契约与接线；下列每个端点由后续任务填充。
func notImplemented(c *gin.Context) { respond.NotImplemented(c.Writer, c.Request) }

// UserCreate 是 P1 占位实现。
func (s *server) UserCreate(c *gin.Context, _ oapi.UserCreateParams) { notImplemented(c) }

// SchoolList 是 P1 占位实现。
func (s *server) SchoolList(c *gin.Context, params oapi.SchoolListParams) { notImplemented(c) }

// SchoolCreate 是 P1 占位实现。
func (s *server) SchoolCreate(c *gin.Context, _ oapi.SchoolCreateParams) { notImplemented(c) }

// SchoolUpdate 是 P1 占位实现。
func (s *server) SchoolUpdate(c *gin.Context, id oapi.SchoolID, _ oapi.SchoolUpdateParams) {
	notImplemented(c)
}

// SchoolAdminList 是 P1 占位实现。
func (s *server) SchoolAdminList(c *gin.Context, id oapi.SchoolID, params oapi.SchoolAdminListParams) {
	notImplemented(c)
}

// SchoolAdminAdd 是 P1 占位实现。
func (s *server) SchoolAdminAdd(c *gin.Context, id oapi.SchoolID, _ oapi.SchoolAdminAddParams) {
	notImplemented(c)
}

// SchoolAdminRemove 是 P1 占位实现。
func (s *server) SchoolAdminRemove(c *gin.Context, id oapi.SchoolID, uid oapi.UserID, _ oapi.SchoolAdminRemoveParams) {
	notImplemented(c)
}

// AccountList 是 P1 占位实现。
func (s *server) AccountList(c *gin.Context, params oapi.AccountListParams) { notImplemented(c) }

// AccountCreate 是 P1 占位实现。
func (s *server) AccountCreate(c *gin.Context, _ oapi.AccountCreateParams) { notImplemented(c) }

// AccountGet 是 P1 占位实现。
func (s *server) AccountGet(c *gin.Context, id oapi.AccountID) { notImplemented(c) }

// AccountUpdate 是 P1 占位实现。
func (s *server) AccountUpdate(c *gin.Context, id oapi.AccountID, _ oapi.AccountUpdateParams) {
	notImplemented(c)
}

// AccountDelete 是 P1 占位实现。
func (s *server) AccountDelete(c *gin.Context, id oapi.AccountID, _ oapi.AccountDeleteParams) {
	notImplemented(c)
}

// AccountReview 是 P1 占位实现。
func (s *server) AccountReview(c *gin.Context, id oapi.AccountID, _ oapi.AccountReviewParams) {
	notImplemented(c)
}

// AccountPause 是 P1 占位实现。
func (s *server) AccountPause(c *gin.Context, id oapi.AccountID, _ oapi.AccountPauseParams) {
	notImplemented(c)
}
