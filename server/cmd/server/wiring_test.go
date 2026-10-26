package main

import (
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/storage"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	appadmin "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/admin"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/auth"
	appdiagnostic "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/diagnostic"
	appegress "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/egress"
	appevents "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/events"
	applocation "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/location"
	appphotos "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/photos"
	appschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schedule"
	appschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/school"
	apptimepref "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/timepref"
	appuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/user"
	appworker "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/worker"
	corerate "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/rate"
	eventshttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/events"
	locationhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/location"
	photoshttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/photos"
	schedulehttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/schedule"
	signinghttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/signing"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/providers/yiban"
)

// 编译期接线检查。
//
// 端口在 internal/app，实现在 internal/adapters/pg。
// canonical.tsv 禁止适配器导入 app，
// 故这些断言是两侧必须对齐的地方。
// 任一侧签名改动都会在编译期失败，而不是留到首次调用。
//
// 本文件只断言类型。它**不**建连接，**不**启动服务。
var (
	_ app.TxManager             = (*pg.DB)(nil)
	_ appuser.Repo              = (*pg.UserRepo)(nil)
	_ appauth.SessionRepo       = (*pg.SessionRepo)(nil)
	_ appauth.PasswordResetRepo = (*pg.PasswordResetRepo)(nil)
	_ appschool.Repo            = (*pg.SchoolRepo)(nil)
	_ appschool.AdminRepo       = (*pg.AdminSchoolRepo)(nil)
	_ appaccount.Repo           = (*pg.AccountRepo)(nil)
	_ appaudit.Repo             = (*pg.AuditRepo)(nil)
	_ appschedule.WindowStore   = (*pg.ScheduleRepo)(nil)
	_ appschedule.PlanStore     = (*pg.ScheduleRepo)(nil)
	_ appschedule.Accounts      = (*pg.ScheduleRepo)(nil)
	_ appegress.Store           = (*pg.EgressRepo)(nil)
	_ appworker.Store           = (*pg.ScheduleRepo)(nil)
	_ appworker.Signer          = (*yiban.Provider)(nil)
	_ appworker.Egress          = (*appegress.Service)(nil)
	_ appworker.RateGate        = (*corerate.Limiter)(nil)
	// P4-F：自选仓储同时满足计划层与自选用例两个端口。
	_ appschedule.TimePrefs = (*pg.TimePrefRepo)(nil)
	_ apptimepref.Store     = (*pg.TimePrefRepo)(nil)
	// P4-F：HTTP 处理器的两个用例端口。
	_ schedulehttp.Planner   = (*appschedule.Service)(nil)
	_ schedulehttp.TimePrefs = (*apptimepref.Service)(nil)
	// 手动签到的可见面判据与入队接缝。
	_ signinghttp.AccountAccess = (*appaccount.Service)(nil)
	_ signinghttp.ManualSigner  = (*appworker.Service)(nil)
	// P5-B：图片仓储与 blob 存储。
	_ appphotos.Store    = (*pg.PhotoRepo)(nil)
	_ appphotos.Blobs    = (*storage.FS)(nil)
	_ photoshttp.Service = (*appphotos.Service)(nil)
	// P5-C：定位策略与 worker 的两个注入端口。
	_ applocation.Store        = (*pg.LocationRepo)(nil)
	_ applocation.Accounts     = (*appaccount.Service)(nil)
	_ locationhttp.Service     = (*applocation.Service)(nil)
	_ appworker.LocationSource = (*applocation.Service)(nil)
	_ appworker.PhotoSource    = (*appphotos.Service)(nil)
	// P6：事件总线、发布端口与诊断读模型。
	_ appworker.Notifier          = workerNotifier{}
	_ appschedule.Notifier        = scheduleNotifier{}
	_ appdiagnostic.Store         = (*pg.DiagnosticRepo)(nil)
	_ appdiagnostic.AccountSource = (*appaccount.Service)(nil)
	_ eventshttp.Broker           = (*appevents.Broker)(nil)
	_ eventshttp.Diagnostics      = (*appdiagnostic.Service)(nil)
	_ eventshttp.Authorizer       = (*appadmin.Service)(nil)
)
