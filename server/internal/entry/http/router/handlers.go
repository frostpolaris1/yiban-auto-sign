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

// UserList 委托用户处理器。未装配时仍是 501。
func (s *server) UserList(c *gin.Context, params oapi.UserListParams) {
	if s.users == nil {
		notImplemented(c)
		return
	}
	s.users.List(c, params)
}

// UserCreate 委托用户处理器。未装配时仍是 501。
func (s *server) UserCreate(c *gin.Context, params oapi.UserCreateParams) {
	if s.users == nil {
		notImplemented(c)
		return
	}
	s.users.Create(c, params)
}

// UserUpdate 委托用户处理器。未装配时仍是 501。
func (s *server) UserUpdate(c *gin.Context, id string, params oapi.UserUpdateParams) {
	if s.users == nil {
		notImplemented(c)
		return
	}
	s.users.Update(c, id, params)
}

// UserDelete 委托用户处理器。未装配时仍是 501。
func (s *server) UserDelete(c *gin.Context, id string, params oapi.UserDeleteParams) {
	if s.users == nil {
		notImplemented(c)
		return
	}
	s.users.Delete(c, id, params)
}

// UserPasswordReset 委托用户处理器。未装配时仍是 501。
func (s *server) UserPasswordReset(c *gin.Context, id string, params oapi.UserPasswordResetParams) {
	if s.users == nil {
		notImplemented(c)
		return
	}
	s.users.PasswordReset(c, id, params)
}

// UserRestore 委托用户处理器。未装配时仍是 501。
func (s *server) UserRestore(c *gin.Context, id string, params oapi.UserRestoreParams) {
	if s.users == nil {
		notImplemented(c)
		return
	}
	s.users.Restore(c, id, params)
}

// UserLookup 委托用户处理器。未装配时仍是 501。
func (s *server) UserLookup(c *gin.Context, params oapi.UserLookupParams) {
	if s.users == nil {
		notImplemented(c)
		return
	}
	s.users.Lookup(c, params)
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

// AccountSummary 委托账号处理器（静态段，紧邻 {id} 路由）。未装配时仍是 501。
func (s *server) AccountSummary(c *gin.Context) {
	if s.accounts == nil {
		notImplemented(c)
		return
	}
	s.accounts.Summary(c)
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

// AccountRestore 委托账号处理器。未装配时仍是 501。
func (s *server) AccountRestore(c *gin.Context, id oapi.AccountID, params oapi.AccountRestoreParams) {
	if s.accounts == nil {
		notImplemented(c)
		return
	}
	s.accounts.Restore(c, id, params)
}

// MyAccountDelete 委托账号处理器。未装配时仍是 501。
func (s *server) MyAccountDelete(c *gin.Context, id oapi.AccountID, params oapi.MyAccountDeleteParams) {
	if s.accounts == nil {
		notImplemented(c)
		return
	}
	s.accounts.MyAccountDelete(c, id, params)
}

// MyAccountRestore 委托账号处理器。未装配时仍是 501。
func (s *server) MyAccountRestore(c *gin.Context, id oapi.AccountID, params oapi.MyAccountRestoreParams) {
	if s.accounts == nil {
		notImplemented(c)
		return
	}
	s.accounts.MyAccountRestore(c, id, params)
}

// MailNotifyGet 委托用户处理器。未装配时仍是 501。
func (s *server) MailNotifyGet(c *gin.Context) {
	if s.users == nil {
		notImplemented(c)
		return
	}
	s.users.MailNotifyGet(c)
}

// MailNotifySet 委托用户处理器。未装配时仍是 501。
func (s *server) MailNotifySet(c *gin.Context, params oapi.MailNotifySetParams) {
	if s.users == nil {
		notImplemented(c)
		return
	}
	s.users.MailNotifySet(c, params)
}

// AccountDeviceRefresh 委托账号处理器。未装配时仍是 501。
func (s *server) AccountDeviceRefresh(c *gin.Context, id oapi.AccountID, params oapi.AccountDeviceRefreshParams) {
	if s.accounts == nil {
		notImplemented(c)
		return
	}
	s.accounts.DeviceRefresh(c, id, params)
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
	if !s.accountLimit(c, string(id)) {
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
	if !s.accountLimit(c, string(id)) {
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

// SignStats 委托签到统计处理器。未装配时仍是 501。
func (s *server) SignStats(c *gin.Context, params oapi.SignStatsParams) {
	if s.schedule == nil {
		notImplemented(c)
		return
	}
	s.schedule.SignStats(c, params)
}

// TimePrefStats 委托自选时间片统计处理器。未装配时仍是 501。
func (s *server) TimePrefStats(c *gin.Context, params oapi.TimePrefStatsParams) {
	if s.schedule == nil {
		notImplemented(c)
		return
	}
	s.schedule.TimePrefStats(c, params)
}

// AdminTaskList 委托任务检索处理器。未装配时仍是 501。
func (s *server) AdminTaskList(c *gin.Context, params oapi.AdminTaskListParams) {
	if s.tasks == nil {
		notImplemented(c)
		return
	}
	s.tasks.List(c, params)
}

// AdminTaskExport 委托任务导出处理器。未装配时仍是 501。
func (s *server) AdminTaskExport(c *gin.Context, params oapi.AdminTaskExportParams) {
	if s.tasks == nil {
		notImplemented(c)
		return
	}
	s.tasks.Export(c, params)
}

// AdminEngine 委托执行监控处理器。未装配时仍是 501。
func (s *server) AdminEngine(c *gin.Context) {
	if s.engine == nil {
		notImplemented(c)
		return
	}
	s.engine.Snapshot(c)
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
func (s *server) PhotoGet(c *gin.Context, id oapi.PhotoID, params oapi.PhotoGetParams) {
	if s.photos == nil {
		notImplemented(c)
		return
	}
	s.photos.PhotoGet(c, id, params)
}

// PhotoDelete 委托图片处理器。未装配时仍是 501。
func (s *server) PhotoDelete(c *gin.Context, id oapi.PhotoID, params oapi.PhotoDeleteParams) {
	if s.photos == nil {
		notImplemented(c)
		return
	}
	s.photos.PhotoDelete(c, id, params)
}

// PhotoRetentionGet 委托图片处理器。未装配时仍是 501。
func (s *server) PhotoRetentionGet(c *gin.Context) {
	if s.photos == nil {
		notImplemented(c)
		return
	}
	s.photos.PhotoRetentionGet(c)
}

// PhotoRetentionSet 委托图片处理器。未装配时仍是 501。
func (s *server) PhotoRetentionSet(c *gin.Context, params oapi.PhotoRetentionSetParams) {
	if s.photos == nil {
		notImplemented(c)
		return
	}
	s.photos.PhotoRetentionSet(c, params)
}

// AccountPhotoList 委托按账号列照片处理器。未装配时仍是 501。
func (s *server) AccountPhotoList(c *gin.Context, id oapi.AccountID, params oapi.AccountPhotoListParams) {
	if s.photos == nil {
		notImplemented(c)
		return
	}
	s.photos.AccountPhotoList(c, id, params)
}

// SchoolDefaultPhotoGet 委托学校默认图读取处理器。未装配时仍是 501。
func (s *server) SchoolDefaultPhotoGet(c *gin.Context, id oapi.SchoolID) {
	if s.photos == nil {
		notImplemented(c)
		return
	}
	s.photos.SchoolDefaultPhotoGet(c, id)
}

// SchoolDefaultPhotoSet 委托学校默认图写入处理器。未装配时仍是 501。
func (s *server) SchoolDefaultPhotoSet(c *gin.Context, id oapi.SchoolID, params oapi.SchoolDefaultPhotoSetParams) {
	if s.photos == nil {
		notImplemented(c)
		return
	}
	s.photos.SchoolDefaultPhotoSet(c, id, params)
}

// AdminEvents 委托事件流处理器。未装配时仍是 501。
func (s *server) AdminEvents(c *gin.Context, params oapi.AdminEventsParams) {
	if s.events == nil {
		notImplemented(c)
		return
	}
	s.events.AdminEvents(c, params)
}

// MeDiagnostics 委托诊断处理器。未装配时仍是 501。
func (s *server) MeDiagnostics(c *gin.Context) {
	if s.events == nil {
		notImplemented(c)
		return
	}
	s.events.MeDiagnostics(c)
}

// AccountDiagnostics 委托诊断处理器。未装配时仍是 501。
func (s *server) AccountDiagnostics(c *gin.Context, id oapi.AccountID) {
	if s.events == nil {
		notImplemented(c)
		return
	}
	s.events.AccountDiagnostics(c, id)
}

// MeSignStatus 委托轮询处理器。未装配时仍是 501。
func (s *server) MeSignStatus(c *gin.Context) {
	if s.events == nil {
		notImplemented(c)
		return
	}
	s.events.MeSignStatus(c)
}

// SignCalendarGet 委托日历处理器。未装配时仍是 501。
func (s *server) SignCalendarGet(c *gin.Context, params oapi.SignCalendarGetParams) {
	if s.calendar == nil {
		notImplemented(c)
		return
	}
	s.calendar.SignCalendarGet(c, params)
}

// SignCalendarDayGet 委托日历处理器。未装配时仍是 501。
func (s *server) SignCalendarDayGet(c *gin.Context, day string) {
	if s.calendar == nil {
		notImplemented(c)
		return
	}
	s.calendar.SignCalendarDayGet(c, day)
}

// AdminNotifyGet 委托通知订阅读取。未装配时仍是 501。
func (s *server) AdminNotifyGet(c *gin.Context) {
	if s.notify == nil {
		notImplemented(c)
		return
	}
	s.notify.AdminNotifyGet(c)
}

// AdminNotifySet 委托通知订阅替换。未装配时仍是 501。
func (s *server) AdminNotifySet(c *gin.Context, params oapi.AdminNotifySetParams) {
	if s.notify == nil {
		notImplemented(c)
		return
	}
	s.notify.AdminNotifySet(c, params)
}

// NotificationList 委托站内信列表。未装配时仍是 501。
func (s *server) NotificationList(c *gin.Context, params oapi.NotificationListParams) {
	if s.notify == nil {
		notImplemented(c)
		return
	}
	s.notify.NotificationList(c, params)
}

// NotificationUnreadCount 委托未读数读取。未装配时仍是 501。
func (s *server) NotificationUnreadCount(c *gin.Context) {
	if s.notify == nil {
		notImplemented(c)
		return
	}
	s.notify.NotificationUnreadCount(c)
}

// NotificationMarkRead 委托标记已读。未装配时仍是 501。
func (s *server) NotificationMarkRead(c *gin.Context, params oapi.NotificationMarkReadParams) {
	if s.notify == nil {
		notImplemented(c)
		return
	}
	s.notify.NotificationMarkRead(c, params)
}

// NotificationMarkAllRead 委托全部已读。未装配时仍是 501。
func (s *server) NotificationMarkAllRead(c *gin.Context, params oapi.NotificationMarkAllReadParams) {
	if s.notify == nil {
		notImplemented(c)
		return
	}
	s.notify.NotificationMarkAllRead(c, params)
}

// AdminSettings 委托设置名册读取。未装配时仍是 501。
func (s *server) AdminSettings(c *gin.Context) {
	if s.system == nil {
		notImplemented(c)
		return
	}
	s.system.AdminSettings(c)
}

// AdminMailTest 委托测试邮件端点。未装配时仍是 501。
func (s *server) AdminMailTest(c *gin.Context, params oapi.AdminMailTestParams) {
	if s.system == nil {
		notImplemented(c)
		return
	}
	s.system.AdminMailTest(c, params)
}

// AnnouncementGet 委托公告读取。未装配时仍是 501。
func (s *server) AnnouncementGet(c *gin.Context) {
	if s.system == nil {
		notImplemented(c)
		return
	}
	s.system.AnnouncementGet(c)
}

// AnnouncementSet 委托公告写入。未装配时仍是 501。
func (s *server) AnnouncementSet(c *gin.Context, params oapi.AnnouncementSetParams) {
	if s.system == nil {
		notImplemented(c)
		return
	}
	s.system.AnnouncementSet(c, params)
}

// AuditLogList 委托审计列表。未装配时仍是 501。
func (s *server) AuditLogList(c *gin.Context, params oapi.AuditLogListParams) {
	if s.audit == nil {
		notImplemented(c)
		return
	}
	s.audit.AuditLogList(c, params)
}
