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
// 它只服务「域处理器未装配」的端点；生产装配后不应出现。
func notImplemented(c *gin.Context) { respond.NotImplemented(c.Writer, c.Request) }

// UserCreate 委托用户处理器。未装配时仍是 501。
func (s *server) UserCreate(c *gin.Context, params oapi.UserCreateParams) {
	if s.users == nil {
		notImplemented(c)
		return
	}
	s.users.Create(c, params)
}

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

// AccountSign 委托手动签到处理器。未装配时仍是 501。
func (s *server) AccountSign(c *gin.Context, id oapi.AccountID, params oapi.AccountSignParams) {
	if s.signing == nil {
		notImplemented(c)
		return
	}
	s.signing.Sign(c, id, params)
}

// AccountLocationSet 委托定位策略处理器。未装配时仍是 501。
func (s *server) AccountLocationSet(c *gin.Context, id oapi.AccountID, params oapi.AccountLocationSetParams) {
	if s.locations == nil {
		notImplemented(c)
		return
	}
	s.locations.SetLocation(c, id, params)
}

// AccountVerify 委托签到凭据校验处理器。未装配时仍是 501。
func (s *server) AccountVerify(c *gin.Context, id oapi.AccountID, params oapi.AccountVerifyParams) {
	if s.signing == nil {
		notImplemented(c)
		return
	}
	s.signing.Verify(c, id, params)
}

// ScheduleWindows 委托计划处理器。未装配时仍是 501。
func (s *server) ScheduleWindows(c *gin.Context, params oapi.ScheduleWindowsParams) {
	if s.schedule == nil {
		notImplemented(c)
		return
	}
	s.schedule.Windows(c, params)
}

// SchedulePreview 委托计划处理器。未装配时仍是 501。
func (s *server) SchedulePreview(c *gin.Context, params oapi.SchedulePreviewParams) {
	if s.schedule == nil {
		notImplemented(c)
		return
	}
	s.schedule.Preview(c, params)
}

// SchedulePlan 委托计划处理器。未装配时仍是 501。
func (s *server) SchedulePlan(c *gin.Context, params oapi.SchedulePlanParams) {
	if s.schedule == nil {
		notImplemented(c)
		return
	}
	s.schedule.Plan(c, params)
}

// ScheduleOverride 委托计划处理器。未装配时仍是 501。
func (s *server) ScheduleOverride(c *gin.Context, params oapi.ScheduleOverrideParams) {
	if s.schedule == nil {
		notImplemented(c)
		return
	}
	s.schedule.Override(c, params)
}

// CapacityGet 委托容量处理器。未装配时仍是 501。
func (s *server) CapacityGet(c *gin.Context, params oapi.CapacityGetParams) {
	if s.schedule == nil {
		notImplemented(c)
		return
	}
	s.schedule.CapacityReport(c, params)
}

// TimePrefGet 委托自选时间片处理器。未装配时仍是 501。
func (s *server) TimePrefGet(c *gin.Context) {
	if s.schedule == nil {
		notImplemented(c)
		return
	}
	s.schedule.TimePrefGet(c)
}

// TimePrefSet 委托自选时间片处理器。未装配时仍是 501。
func (s *server) TimePrefSet(c *gin.Context, params oapi.TimePrefSetParams) {
	if s.schedule == nil {
		notImplemented(c)
		return
	}
	s.schedule.TimePrefSet(c, params)
}

// PhotoList 委托图片处理器。未装配时仍是 501。
func (s *server) PhotoList(c *gin.Context, params oapi.PhotoListParams) {
	if s.photos == nil {
		notImplemented(c)
		return
	}
	s.photos.PhotoList(c, params)
}

// PhotoUpload 委托图片处理器。未装配时仍是 501。
func (s *server) PhotoUpload(c *gin.Context, params oapi.PhotoUploadParams) {
	if s.photos == nil {
		notImplemented(c)
		return
	}
	s.photos.PhotoUpload(c, params)
}

// PhotoGet 委托图片处理器。未装配时仍是 501。
func (s *server) PhotoGet(c *gin.Context, id oapi.PhotoID) {
	if s.photos == nil {
		notImplemented(c)
		return
	}
	s.photos.PhotoGet(c, id)
}

// PhotoDelete 委托图片处理器。未装配时仍是 501。
func (s *server) PhotoDelete(c *gin.Context, id oapi.PhotoID, params oapi.PhotoDeleteParams) {
	if s.photos == nil {
		notImplemented(c)
		return
	}
	s.photos.PhotoDelete(c, id, params)
}

// PhotoRetentionSet 委托图片处理器。未装配时仍是 501。
func (s *server) PhotoRetentionSet(c *gin.Context, params oapi.PhotoRetentionSetParams) {
	if s.photos == nil {
		notImplemented(c)
		return
	}
	s.photos.PhotoRetentionSet(c, params)
}
