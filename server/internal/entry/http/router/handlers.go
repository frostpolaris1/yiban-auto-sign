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

// UserCreate 是 P1 占位实现。用户管理属后续批次。
func (s *server) UserCreate(c *gin.Context, _ oapi.UserCreateParams) { notImplemented(c) }

// SchoolList 委托院校处理器。未装配时仍是 501。
func (s *server) SchoolList(c *gin.Context, params oapi.SchoolListParams) {
	if s.schools == nil {
		notImplemented(c)
		return
	}
	s.schools.List(c, params)
}

// SchoolCreate 委托院校处理器。未装配时仍是 501。
func (s *server) SchoolCreate(c *gin.Context, params oapi.SchoolCreateParams) {
	if s.schools == nil {
		notImplemented(c)
		return
	}
	s.schools.Create(c, params)
}

// SchoolUpdate 委托院校处理器。未装配时仍是 501。
func (s *server) SchoolUpdate(c *gin.Context, id oapi.SchoolID, params oapi.SchoolUpdateParams) {
	if s.schools == nil {
		notImplemented(c)
		return
	}
	s.schools.Update(c, id, params)
}

// SchoolAdminList 委托管理员处理器。未装配时仍是 501。
func (s *server) SchoolAdminList(c *gin.Context, id oapi.SchoolID, params oapi.SchoolAdminListParams) {
	if s.admins == nil {
		notImplemented(c)
		return
	}
	s.admins.List(c, id, params)
}

// SchoolAdminAdd 委托管理员处理器。未装配时仍是 501。
func (s *server) SchoolAdminAdd(c *gin.Context, id oapi.SchoolID, params oapi.SchoolAdminAddParams) {
	if s.admins == nil {
		notImplemented(c)
		return
	}
	s.admins.Add(c, id, params)
}

// SchoolAdminRemove 委托管理员处理器。未装配时仍是 501。
func (s *server) SchoolAdminRemove(c *gin.Context, id oapi.SchoolID, uid oapi.UserID, params oapi.SchoolAdminRemoveParams) {
	if s.admins == nil {
		notImplemented(c)
		return
	}
	s.admins.Remove(c, id, uid, params)
}

// AccountList 委托账号处理器。未装配时仍是 501。
func (s *server) AccountList(c *gin.Context, params oapi.AccountListParams) {
	if s.accounts == nil {
		notImplemented(c)
		return
	}
	s.accounts.List(c, params)
}

// AccountCreate 委托账号处理器。未装配时仍是 501。
func (s *server) AccountCreate(c *gin.Context, params oapi.AccountCreateParams) {
	if s.accounts == nil {
		notImplemented(c)
		return
	}
	s.accounts.Create(c, params)
}

// AccountGet 委托账号处理器。未装配时仍是 501。
func (s *server) AccountGet(c *gin.Context, id oapi.AccountID) {
	if s.accounts == nil {
		notImplemented(c)
		return
	}
	s.accounts.Get(c, id)
}

// AccountUpdate 委托账号处理器。未装配时仍是 501。
func (s *server) AccountUpdate(c *gin.Context, id oapi.AccountID, params oapi.AccountUpdateParams) {
	if s.accounts == nil {
		notImplemented(c)
		return
	}
	s.accounts.Update(c, id, params)
}

// AccountDelete 委托账号处理器。未装配时仍是 501。
func (s *server) AccountDelete(c *gin.Context, id oapi.AccountID, params oapi.AccountDeleteParams) {
	if s.accounts == nil {
		notImplemented(c)
		return
	}
	s.accounts.Delete(c, id, params)
}

// AccountReview 委托账号处理器。未装配时仍是 501。
func (s *server) AccountReview(c *gin.Context, id oapi.AccountID, params oapi.AccountReviewParams) {
	if s.accounts == nil {
		notImplemented(c)
		return
	}
	s.accounts.Review(c, id, params)
}

// AccountPause 委托账号处理器。未装配时仍是 501。
func (s *server) AccountPause(c *gin.Context, id oapi.AccountID, params oapi.AccountPauseParams) {
	if s.accounts == nil {
		notImplemented(c)
		return
	}
	s.accounts.Pause(c, id, params)
}
