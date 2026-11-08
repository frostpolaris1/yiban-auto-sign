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
// entry/http 入口层。作用域由 Authorizer（app/schooladmin）解析；
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
	"math"
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
	// SelfContext 解析用户自选读面的当日账号与任务；TaskID 不得下发。
	SelfContext(ctx context.Context, userID, day string) (coreschedule.SelfContext, error)
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
	// 用户自选读面与这里共用 slotFrames：同一窗口必得同一 offset/label/count。
	frames := h.slotFrames(window, counts)
	slots := make([]oapi.PrefStatsSlot, len(frames))
	for i, frame := range frames {
		slots[i] = oapi.PrefStatsSlot{
			OffsetMin: frame.OffsetMin,
			Label:     frame.Label,
			Count:     frame.Count,
			Cap:       h.cfg.SlotsPerSlice,
		}
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

// prefSlotFrame 是一个 5 分钟槽的传输前形状：管理端统计与用户读面共用。
// 两处必须由同一函数产出，拥挤度的 offset/label/count 才不会各算一套。
type prefSlotFrame struct {
	OffsetMin int
	Label     string
	Count     int
}

// slotFrames 把窗口切成整 5 分钟片（floor，最多 24 片，不足一片夹到 1 片）。
// 与计划同源：ceil 会多出计划永远用不到的尾槽；不足一片时计划也落 offset 0。
func (h *Handler) slotFrames(window appschedule.Preview, counts map[int]int) []prefSlotFrame {
	slices := int(window.WindowEnd.Sub(window.WindowStart) / (5 * time.Minute))
	if slices > 24 {
		slices = 24
	}
	if slices < 1 {
		slices = 1
	}
	frames := make([]prefSlotFrame, 0, slices)
	for index := 0; index < slices; index++ {
		offset := index * 5
		at := window.WindowStart.Add(time.Duration(offset) * time.Minute)
		frames = append(frames, prefSlotFrame{
			OffsetMin: offset,
			Label:     at.In(h.cfg.Location).Format("15:04"),
			Count:     counts[offset],
		})
	}
	return frames
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
	out, err := h.timePrefResponse(c, view, user.ID)
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, 200, out)
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
	out, err := h.timePrefResponse(c, view, user.ID)
	if err != nil {
		// 写入已经成功：响应组装失败（窗口/拥挤度读面）不得报成写入失败。
		// 回基础视图；estimated 若已取到则保留。客户端可再 GET 补全。
		respond.JSON(c.Writer, 200, out)
		return
	}
	respond.JSON(c.Writer, 200, out)
}

// timePrefResponse 在基础自选视图上补齐账号任务窗口、拥挤度与台账预计时段。
//
// 窗口与拥挤度只走既有读路径：Report 与 admin time-prefs/stats 同源；
// slotFrames 与 admin 端点共用。用户不传 task_id，响应也不回 task_id、
// school_id 或 account_id。
// 无生效账号、无启用任务或窗口不可发现时保持基础视图：window 是服务端标准
// 窗口（计划回退同常量），slots 沿用标准窗口、pct 记 0，estimated=null。
func (h *Handler) timePrefResponse(c *gin.Context, view apptimepref.View, userID string) (oapi.TimePref, error) {
	out := timePrefView(view)
	self, err := h.planner.SelfContext(c.Request.Context(), userID, h.today())
	if err != nil {
		return out, err
	}
	if self.RunAt != nil {
		estimated := self.RunAt.In(h.cfg.Location).Format("15:04")
		out.Estimated = &estimated
		out.EstimateNote = "（今日计划，非承诺）"
	}
	if !self.HasTask {
		return out, nil
	}
	window, err := h.planner.Report(c.Request.Context(), h.input(h.today(), self.SchoolID, self.TaskID))
	if err != nil {
		return out, err
	}
	if h.stats == nil {
		return out, errs.New(errs.CodeInternal, "stats service is not wired")
	}
	counts, err := h.stats.OffsetCounts(c.Request.Context(), self.SchoolID)
	if err != nil {
		return out, err
	}
	out.Window = oapi.TimePrefWindow{
		Start: window.WindowStart.In(h.cfg.Location).Format("15:04"),
		End:   window.WindowEnd.In(h.cfg.Location).Format("15:04"),
	}
	slots := h.crowdedSlots(window, counts, view.Allowed)
	out.Slots = &slots
	return out, nil
}

// crowdedSlots 把同一份 slotFrames 转成用户读面：pct 是 10% 档拥挤度，
// 与 admin time-prefs/stats 的 count/cap 同源。
// disabled 只随部署开关；edge_note 只在首/尾片且部署保留边距时出现。
func (h *Handler) crowdedSlots(window appschedule.Preview, counts map[int]int, allowed bool) []oapi.TimePrefSlot {
	frames := h.slotFrames(window, counts)
	slots := make([]oapi.TimePrefSlot, len(frames))
	for i, frame := range frames {
		slot := oapi.TimePrefSlot{
			OffsetMin: frame.OffsetMin,
			Label:     frame.Label,
			Pct:       crowdPct(frame.Count, h.cfg.SlotsPerSlice),
		}
		if !allowed {
			slot.Disabled = true
			reason := "time preference is disabled"
			slot.Reason = &reason
		}
		if slot.Disabled {
			slots[i] = slot
			continue
		}
		switch {
		case i == 0 && h.cfg.EdgeFront > 0:
			slot.EdgeNote = "开头 " + durationMinutes(h.cfg.EdgeFront) + " 分钟保留"
		case i == len(frames)-1 && i != 0 && h.cfg.EdgeBack > 0:
			slot.EdgeNote = "结尾 " + durationMinutes(h.cfg.EdgeBack) + " 分钟保留"
		}
		slots[i] = slot
	}
	return slots
}

// crowdPct 复刻旧线用户端口径：10% 一档，满员恰好 100，未满封顶 90；
// 有 1 人至少 10，0 人 0。分母与 admin stats 的 cap 同源（SlotsPerSlice）。
// 粗粒度是防调研：精确百分比加已知容量可反推人数。
func crowdPct(count, capacity int) int {
	if capacity < 1 {
		capacity = 1
	}
	if count >= capacity {
		return 100
	}
	if count <= 0 {
		return 0
	}
	pct := int(math.RoundToEven(float64(count*10)/float64(capacity))) * 10
	if pct < 10 {
		return 10
	}
	if pct > 90 {
		return 90
	}
	return pct
}

// durationMinutes 把边距时长写成旧线文案里的分钟数：1、0.5、2。
func durationMinutes(d time.Duration) string {
	return strconv.FormatFloat(float64(d)/float64(time.Minute), 'g', -1, 64)
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

// timePrefView 把自选读数转成基础传输形状。
// slots 始终由服务端生成：窗口为空时省略整个键，而不是给空数组。
// 基础形状没有账号上下文：pct 记 0（无拥挤度数据），edge_note 空，
// estimated=null 且 estimate_note=待生成；入口层取到任务后再覆写这些字段。
func timePrefView(v apptimepref.View) oapi.TimePref {
	out := oapi.TimePref{
		OffsetMin:    v.OffsetMin,
		Allowed:      v.Allowed,
		EstimateNote: "待生成",
		Window: oapi.TimePrefWindow{
			Start: v.Window.Start,
			End:   v.Window.End,
		},
	}
	if len(v.Slots) > 0 {
		slots := make([]oapi.TimePrefSlot, len(v.Slots))
		for i, slot := range v.Slots {
			slots[i] = oapi.TimePrefSlot{
				OffsetMin: slot.OffsetMin,
				Label:     slot.Label,
				Disabled:  slot.Disabled,
				Pct:       0,
				EdgeNote:  "",
			}
			if slot.Reason != "" {
				reason := slot.Reason
				slots[i].Reason = &reason
			}
		}
		out.Slots = &slots
	}
	return out
}

// fail 写标准错误信封。
func fail(c *gin.Context, err error) {
	respond.Error(c.Writer, c.Request, err)
}
