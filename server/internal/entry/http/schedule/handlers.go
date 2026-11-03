// Package schedule 把计划、容量、自选时间片、任务检索与执行监控用例映射到 HTTP。
//
// # 功能
// 计划面七个端点：窗口列表、计划预览、计划落库、窗口覆盖、容量报告、
// 自选时间片读取与写入。任务检索面两个端点（UX-3）：任务列表与
// CSV 导出，见 tasks.go 的 TasksHandler。执行监控一个端点（UX-5）：
// 单进程快照，见 engine.go 的 EngineHandler。
// 本包**不**查库、**不**算几何、**不**决定窗口来源。
//
// # 归属
// entry/http 入口层。作用域由 Authorizer（app/admin）解析；
// 计划、自选与任务检索分别由 app/schedule、app/timepref 提供。
//
// # 复用
// Handler、NewHandler、Config、Planner、TimePrefs、Authorizer。
//
// # 口径
// 计划管理面只给 master 与 admin；自选对任意已认证主体开放，
// 偏好行按会话 userID 归属，角色不参与判定。
// 跨校任务一律 404；响应不含凭据与手机号。
// 停用任务可预览，建计划与写覆盖回 409（k0y-6）。
// 窗口列表与容量默认只读缓存，refresh=true 才重新发现（k0y-4）。
package schedule

import (
	"context"
	"encoding/json"
	"io"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
	openapi_types "github.com/oapi-codegen/runtime/types"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schedule"
	appstats "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/stats"
	apptimepref "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/timepref"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
)

// Planner 是计划用例端口（实现为 app/schedule.Service）。
type Planner interface {
	Preview(ctx context.Context, in appschedule.Input) (appschedule.Preview, error)
	Build(ctx context.Context, in appschedule.Input) (appschedule.Preview, error)
	Override(ctx context.Context, scope corescope.Scope, in appschedule.Input,
		start, end time.Time, reason, actorID string) (appschedule.Preview, error)
	Report(ctx context.Context, in appschedule.Input) (appschedule.Preview, error)
	Task(ctx context.Context, scope corescope.Scope, taskID string) (coreschedule.TaskInfo, error)
	ListWindows(ctx context.Context, scope corescope.Scope, day string, refresh bool) ([]coreschedule.WindowView, error)
}

// TimePrefs 是自选用例端口（实现为 app/timepref.Service）。
type TimePrefs interface {
	Get(ctx context.Context, userID string) (apptimepref.View, error)
	Set(ctx context.Context, userID string, offset *int) (apptimepref.View, error)
}

// Stats 是看板聚合端口（实现为 app/stats.Service）。
type Stats interface {
	SignStats(ctx context.Context, scope corescope.Scope, from, to string) (appstats.Report, error)
	OffsetCounts(ctx context.Context, schoolID string) (map[int]int, error)
}

// Authorizer 把会话主体解析成作用域与审计主体。
type Authorizer interface {
	Authorize(ctx context.Context, user coreauth.User) (corescope.Scope, appaudit.Actor, error)
}

// Config 是计划参数的装配值。Clock 为空时用系统墙钟。
// SlotsPerSlice 由调用方按"一个 5 分钟片的原始吞吐"算好，
// 与容量公式同源（capacity.Capacity(SlotSpan, ..., util=1)）。
type Config struct {
	Location      *time.Location
	Clock         coreclock.Clock
	Workers       int
	Avg           time.Duration
	Rate          float64
	Util          float64
	SlotsPerSlice int
	Front         bool
	UsePrefs      bool
	EdgeFront     time.Duration
	EdgeBack      time.Duration
}

// Handler 处理 /api/schedule/*、/api/capacity 与 /api/me/time-pref。
type Handler struct {
	planner Planner
	prefs   TimePrefs
	stats   Stats
	authz   Authorizer
	cfg     Config
}

// NewHandler 构造处理器。缺少 Location 时按 UTC；服务层仍会校验。
func NewHandler(planner Planner, prefs TimePrefs, authz Authorizer, cfg Config) *Handler {
	if cfg.Location == nil {
		cfg.Location = time.UTC
	}
	if cfg.Clock == nil {
		cfg.Clock = coreclock.System{}
	}
	return &Handler{planner: planner, prefs: prefs, authz: authz, cfg: cfg}
}

// WithStats 注入看板聚合。未注入时两个统计端点回 500。
func (h *Handler) WithStats(stats Stats) *Handler {
	h.stats = stats
	return h
}

// Windows 实现 GET /api/schedule/windows。
func (h *Handler) Windows(c *gin.Context, params oapi.ScheduleWindowsParams) {
	scope, ok := h.principal(c)
	if !ok {
		return
	}
	day := ""
	if params.Day != nil {
		day = *params.Day
	}
	if day == "" {
		day = h.today()
	}
	views, err := h.planner.ListWindows(c.Request.Context(), scope, day, refreshFlag(params.Refresh))
	if err != nil {
		fail(c, err)
		return
	}
	items := make([]oapi.ScheduleWindow, len(views))
	for i, v := range views {
		items[i] = windowView(v)
	}
	respond.JSON(c.Writer, 200, oapi.ScheduleWindowList{Day: day, Items: items})
}

// Preview 实现 GET /api/schedule/preview。
func (h *Handler) Preview(c *gin.Context, params oapi.SchedulePreviewParams) {
	scope, ok := h.principal(c)
	if !ok {
		return
	}
	task, ok := h.authorizeTask(c, scope, string(params.SchoolId), string(params.TaskId))
	if !ok {
		return
	}
	in := h.input(params.Day, string(params.SchoolId), string(params.TaskId))
	p, err := h.planner.Preview(c.Request.Context(), in)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, 200, planResponse(p, task.Enabled))
}

// Plan 实现 POST /api/schedule/plan。
func (h *Handler) Plan(c *gin.Context, _ oapi.SchedulePlanParams) {
	var body oapi.SchedulePlanRequest
	if err := c.ShouldBindJSON(&body); err != nil {
		respond.BindError(c.Writer, c.Request, err)
		return
	}
	scope, ok := h.principal(c)
	if !ok {
		return
	}
	// 任务归属与作用域先判：跨校任务在建计划前回 404。
	// 停用任务的 409 由用例层给出：判据只在一处（k0y-6）。
	task, ok := h.authorizeTask(c, scope, body.SchoolId, body.TaskId)
	if !ok {
		return
	}
	// 建计划只接受当前业务日：日切由服务端触发，客户端不得补建任意日期。
	if body.Day != h.today() {
		fail(c, errs.New(errs.CodeValidation, "plan day must be the current business day"))
		return
	}
	in := h.input(body.Day, body.SchoolId, body.TaskId)
	p, err := h.planner.Build(c.Request.Context(), in)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, 200, planResponse(p, task.Enabled))
}

// Override 实现 POST /api/schedule/override。
func (h *Handler) Override(c *gin.Context, _ oapi.ScheduleOverrideParams) {
	var body oapi.ScheduleOverrideRequest
	if err := c.ShouldBindJSON(&body); err != nil {
		respond.BindError(c.Writer, c.Request, err)
		return
	}
	scope, user, ok := h.adminPrincipal(c)
	if !ok {
		return
	}
	in := h.input(body.Day, body.SchoolId, body.TaskId)
	p, err := h.planner.Override(c.Request.Context(), scope, in,
		body.Start, body.End, body.Reason, user.ID)
	if err != nil {
		fail(c, err)
		return
	}
	// 覆盖只在启用任务上成功：用例层守卫保证（k0y-6）。
	respond.JSON(c.Writer, 200, planResponse(p, true))
}

// CapacityReport 实现 GET /api/capacity。
// 默认只读缓存；refresh=true 才重新发现平台窗口（k0y-4）。
func (h *Handler) CapacityReport(c *gin.Context, params oapi.CapacityGetParams) {
	scope, ok := h.principal(c)
	if !ok {
		return
	}
	if _, ok := h.authorizeTask(c, scope, string(params.SchoolId), string(params.TaskId)); !ok {
		return
	}
	day := ""
	if params.Day != nil {
		day = *params.Day
	}
	if day == "" {
		day = h.today()
	}
	in := h.input(day, string(params.SchoolId), string(params.TaskId))
	in.Refresh = refreshFlag(params.Refresh)
	p, err := h.planner.Report(c.Request.Context(), in)
	if err != nil {
		fail(c, err)
		return
	}
	report := oapi.CapacityReport{
		SchoolId: in.SchoolID, TaskId: in.SchoolTaskID, Day: p.Day,
		Source:      oapi.ScheduleWindowSource(p.Source),
		WindowStart: p.WindowStart, WindowEnd: p.WindowEnd,
		Capacity: p.Capacity, Demand: p.Demand, Shortfall: p.Shortfall,
	}
	// 两个新字段可选：瓶颈恒有值；建议值 0 表示不给，契约里就不出现。
	if p.Binding != "" {
		binding := oapi.CapacityReportBinding(p.Binding)
		report.Binding = &binding
	}
	if p.SuggestedWorkers > 0 {
		suggested := p.SuggestedWorkers
		report.SuggestedWorkers = &suggested
	}
	respond.JSON(c.Writer, 200, report)
}

// SignStats 实现 GET /api/sign-stats。
func (h *Handler) SignStats(c *gin.Context, params oapi.SignStatsParams) {
	scope, ok := h.principal(c)
	if !ok {
		return
	}
	if h.stats == nil {
		fail(c, errs.New(errs.CodeInternal, "stats service is not wired"))
		return
	}
	days := 30
	if params.Days != nil {
		days = *params.Days
	}
	to := h.cfg.Clock.Now().In(h.cfg.Location)
	toDate := time.Date(to.Year(), to.Month(), to.Day(), 0, 0, 0, 0, time.UTC)
	fromDate := toDate.AddDate(0, 0, -(days - 1))
	report, err := h.stats.SignStats(c.Request.Context(), scope,
		fromDate.Format("2006-01-02"), toDate.Format("2006-01-02"))
	if err != nil {
		fail(c, err)
		return
	}
	out := oapi.SignStatsReport{
		Total: report.Total,
		ByStatus: oapi.SignStatsCounts{
			Pending: report.ByStatus.Pending,
			Running: report.ByStatus.Running,
			Success: report.ByStatus.Success,
			Failed:  report.ByStatus.Failed,
			Skipped: report.ByStatus.Skipped,
		},
		Days:        make([]oapi.SignStatsDay, 0, len(report.Days)),
		DaysMissing: make([]openapi_types.Date, 0, len(report.DaysMissing)),
	}
	for _, day := range report.Days {
		parsed, err := time.Parse("2006-01-02", day.Day)
		if err != nil {
			fail(c, errs.New(errs.CodeInternal, "invalid statistics day"))
			return
		}
		out.Days = append(out.Days, oapi.SignStatsDay{
			Day:     openapi_types.Date{Time: parsed},
			Pending: day.Pending,
			Running: day.Running,
			Success: day.Success,
			Failed:  day.Failed,
			Skipped: day.Skipped,
		})
	}
	for _, day := range report.DaysMissing {
		parsed, err := time.Parse("2006-01-02", day)
		if err != nil {
			fail(c, errs.New(errs.CodeInternal, "invalid statistics day"))
			return
		}
		out.DaysMissing = append(out.DaysMissing, openapi_types.Date{Time: parsed})
	}
	respond.JSON(c.Writer, 200, out)
}

// TimePrefStats 实现 GET /api/time-prefs/stats。
// 窗口来自与容量同一份报告；人数只数该校有活跃账号的用户。
func (h *Handler) TimePrefStats(c *gin.Context, params oapi.TimePrefStatsParams) {
	scope, ok := h.principal(c)
	if !ok {
		return
	}
	if h.stats == nil {
		fail(c, errs.New(errs.CodeInternal, "stats service is not wired"))
		return
	}
	schoolID, taskID := string(params.SchoolId), string(params.TaskId)
	if _, ok := h.authorizeTask(c, scope, schoolID, taskID); !ok {
		return
	}
	day := h.today()
	if params.Day != nil && *params.Day != "" {
		day = *params.Day
	}
	window, err := h.planner.Report(c.Request.Context(), h.input(day, schoolID, taskID))
	if err != nil {
		fail(c, err)
		return
	}
	counts, err := h.stats.OffsetCounts(c.Request.Context(), schoolID)
	if err != nil {
		fail(c, err)
		return
	}
	// 槽数与计划同源：只算整 5 分钟片（floor），最多两小时 24 片。
	// 用 ceil 会多出计划永远用不到的尾槽（placePrefs 对越界偏移自动铺点）。
	slices := int(window.WindowEnd.Sub(window.WindowStart) / (5 * time.Minute))
	if slices > 24 {
		slices = 24
	}
	// 计划对不足一片的窗口夹到 1 片（service.go 的 slices < 1）。
	// 统计必须同源：否则 4 分钟窗口图里 0 槽，账号却落在 offset 0。
	if slices < 1 {
		slices = 1
	}
	slots := make([]oapi.PrefStatsSlot, 0, slices)
	for index := 0; index < slices; index++ {
		offset := index * 5
		at := window.WindowStart.Add(time.Duration(offset) * time.Minute)
		slots = append(slots, oapi.PrefStatsSlot{
			OffsetMin: offset,
			Label:     at.In(h.cfg.Location).Format("15:04"),
			Count:     counts[offset],
			Cap:       h.cfg.SlotsPerSlice,
		})
	}
	respond.JSON(c.Writer, 200, oapi.PrefStatsReport{
		SchoolId:    schoolID,
		TaskId:      taskID,
		Day:         window.Day,
		WindowStart: window.WindowStart,
		WindowEnd:   window.WindowEnd,
		Slots:       slots,
	})
}

// TimePrefGet 实现 GET /api/me/time-pref。
func (h *Handler) TimePrefGet(c *gin.Context) {
	user, ok := h.sessionUser(c)
	if !ok {
		return
	}
	view, err := h.prefs.Get(c.Request.Context(), user.ID)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, 200, timePrefView(view))
}

// TimePrefSet 实现 PUT /api/me/time-pref。
// offset_min 是必填键：缺键回 400；显式 null 才表示清除。
// 键存在性只能在解码原始 JSON 时判定，故这里不用 ShouldBindJSON。
func (h *Handler) TimePrefSet(c *gin.Context, _ oapi.TimePrefSetParams) {
	raw, err := io.ReadAll(c.Request.Body)
	if err != nil {
		// 超过入口上界时是 *http.MaxBytesError；BindError 把它转成 413。
		respond.BindError(c.Writer, c.Request, err)
		return
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil {
		fail(c, errs.New(errs.CodeValidation, "invalid request body"))
		return
	}
	if _, ok := fields["offset_min"]; !ok {
		fail(c, errs.New(errs.CodeValidation, "offset_min is required"))
		return
	}
	var body oapi.TimePrefUpdate
	if err := json.Unmarshal(raw, &body); err != nil {
		fail(c, errs.New(errs.CodeValidation, "invalid request body"))
		return
	}
	user, ok := h.sessionUser(c)
	if !ok {
		return
	}
	view, err := h.prefs.Set(c.Request.Context(), user.ID, body.OffsetMin)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, 200, timePrefView(view))
}

// authorizeTask 判任务在作用域内且属于声明的学校，并返回任务元数据。
// 跨校任务回 404（不区分"没有"与"不给你看"）；
// 同校但 school_id 与 task_id 不配套是参数错误，回 400。
func (h *Handler) authorizeTask(c *gin.Context, scope corescope.Scope, schoolID, taskID string) (coreschedule.TaskInfo, bool) {
	task, err := h.planner.Task(c.Request.Context(), scope, taskID)
	if err != nil {
		fail(c, err)
		return coreschedule.TaskInfo{}, false
	}
	if task.SchoolID != schoolID {
		fail(c, errs.New(errs.CodeValidation, "task does not belong to the school"))
		return coreschedule.TaskInfo{}, false
	}
	return task, true
}

// refreshFlag 把可选的 refresh 查询参数折成布尔值。
// 缺省即只读缓存：只有显式 refresh=true 才重新发现（k0y-4）。
func refreshFlag(v *bool) bool {
	return v != nil && *v
}

// input 组装计划输入。day 为空由调用方决定是否补今天。
func (h *Handler) input(day, schoolID, taskID string) appschedule.Input {
	return appschedule.Input{
		SchoolID:      schoolID,
		SchoolTaskID:  taskID,
		Day:           day,
		Location:      h.cfg.Location,
		Workers:       h.cfg.Workers,
		Avg:           h.cfg.Avg,
		Rate:          h.cfg.Rate,
		Util:          h.cfg.Util,
		SlotsPerSlice: h.cfg.SlotsPerSlice,
		Front:         h.cfg.Front,
		UsePrefs:      h.cfg.UsePrefs,
		EdgeFront:     h.cfg.EdgeFront,
		EdgeBack:      h.cfg.EdgeBack,
	}
}

// today 返回配置时区的业务日。
func (h *Handler) today() string {
	return h.cfg.Clock.Now().In(h.cfg.Location).Format("2006-01-02")
}

// principal 解析作用域；计划管理面只给 master 与 admin。
func (h *Handler) principal(c *gin.Context) (corescope.Scope, bool) {
	scope, _, ok := h.adminPrincipal(c)
	return scope, ok
}

// adminPrincipal 解析作用域与会话主体，并限定 master/admin。
// 角色判据只此一处：预览/计划/容量/覆盖共用。
func (h *Handler) adminPrincipal(c *gin.Context) (corescope.Scope, coreauth.User, bool) {
	return resolveAdminPrincipal(h.authz, c)
}

// resolveAdminPrincipal 是包级判据：计划管理面与任务检索面共用。
// 角色判据只此一处：本包任何 Handler 不得另写一份。
func resolveAdminPrincipal(authz Authorizer, c *gin.Context) (corescope.Scope, coreauth.User, bool) {
	scope, user, ok := resolvePrincipalUser(authz, c)
	if !ok {
		return corescope.Scope{}, coreauth.User{}, false
	}
	if err := app.RequireScope(scope); err != nil {
		fail(c, err)
		return corescope.Scope{}, coreauth.User{}, false
	}
	if kind := scope.Kind(); kind != corescope.KindMaster && kind != corescope.KindAdmin {
		fail(c, errs.New(errs.CodeForbidden, "role cannot manage schedules"))
		return corescope.Scope{}, coreauth.User{}, false
	}
	return scope, user, true
}

// principalUser 解析作用域与会话主体。
func (h *Handler) principalUser(c *gin.Context) (corescope.Scope, coreauth.User, bool) {
	return resolvePrincipalUser(h.authz, c)
}

// resolvePrincipalUser 是包级判据：解析会话并把主体映射成作用域。
func resolvePrincipalUser(authz Authorizer, c *gin.Context) (corescope.Scope, coreauth.User, bool) {
	session, ok := coreauth.FromContext(c.Request.Context())
	if !ok {
		fail(c, errs.New(errs.CodeUnauthorized, "authentication required"))
		return corescope.Scope{}, coreauth.User{}, false
	}
	scope, _, err := authz.Authorize(c.Request.Context(), session.User)
	if err != nil {
		fail(c, err)
		return corescope.Scope{}, coreauth.User{}, false
	}
	return scope, session.User, true
}

// sessionUser 取会话主体。/api/me/time-pref 对任意已认证主体开放：
// 偏好行按会话 userID 归属，角色不参与判定。
func (h *Handler) sessionUser(c *gin.Context) (coreauth.User, bool) {
	_, user, ok := h.principalUser(c)
	if !ok {
		return coreauth.User{}, false
	}
	return user, true
}

// windowView 把领域视图转成传输形状。
func windowView(v coreschedule.WindowView) oapi.ScheduleWindow {
	out := oapi.ScheduleWindow{
		SchoolId: v.SchoolID, SchoolName: v.SchoolName,
		TaskId: v.TaskID, TaskName: v.TaskName, Enabled: v.Enabled,
	}
	day := v.Day
	out.Day = &day
	if v.Cached {
		start, end := v.Start, v.End
		source := v.Source
		version := v.Version
		out.Start, out.End = &start, &end
		out.Source, out.Version = &source, &version
	}
	return out
}

// planResponse 把预览转成传输形状。
// enabled 来自任务元数据：停用任务只可预览，计划响应必须能辨识（k0y-6）。
func planResponse(p appschedule.Preview, enabled bool) oapi.SchedulePlan {
	rows := make([]oapi.ScheduleRow, len(p.Rows))
	for i, r := range p.Rows {
		rows[i] = oapi.ScheduleRow{AccountId: r.AccountID, RunAt: r.RunAt}
	}
	hist := make(map[string]int, len(p.Histogram))
	for k, v := range p.Histogram {
		hist[strconv.Itoa(k)] = v
	}
	return oapi.SchedulePlan{
		Day: p.Day, Enabled: enabled, Source: oapi.ScheduleWindowSource(p.Source),
		WindowStart: p.WindowStart, WindowEnd: p.WindowEnd, Version: p.Version,
		Capacity: p.Capacity, Demand: p.Demand, Shortfall: p.Shortfall,
		Histogram: hist, Rows: rows,
	}
}

// timePrefView 把自选读数转成传输形状。
func timePrefView(v apptimepref.View) oapi.TimePref {
	return oapi.TimePref{OffsetMin: v.OffsetMin, Allowed: v.Allowed}
}

// fail 写标准错误信封。
func fail(c *gin.Context, err error) {
	respond.Error(c.Writer, c.Request, err)
}
