package schedule

// 任务只读检索的两个端点：JSON 列表与 CSV 导出。
//
// # 功能
// TasksHandler.List 实现 GET /api/admin/tasks；
// TasksHandler.Export 实现 GET /api/admin/tasks/export。
// 本包**不**查库、**不**决定作用域；检索用例在 app/schedule.TaskQuery。
//
// # 归属
// entry/http 入口层。作用域由 Authorizer（app/admin）解析，
// 判据与计划管理面同源（resolveAdminPrincipal）。
//
// # 口径
// 两个端点只给 master 与 admin；跨校行不出现，越权 school_id 是空页。
// 列表回 oapi.TaskPage；导出回 text/csv 附件，列与列表同口径，
// 超过 ExportCap 截断并以 X-Export-Truncated: true 显式标记。
// 手机号只用用例给的掩码值；本文件拿不到明文，也不写日志。

import (
	"bytes"
	"context"
	"encoding/csv"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
	openapi_types "github.com/oapi-codegen/runtime/types"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schedule"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
)

// TasksQuerier 是任务检索用例端口（实现为 *appschedule.TaskQuery）。
type TasksQuerier interface {
	List(ctx context.Context, scope corescope.Scope, in coreschedule.TaskQueryFilter) (appschedule.TaskPage, error)
	ExportAll(ctx context.Context, scope corescope.Scope, in coreschedule.TaskQueryFilter) ([]appschedule.TaskView, bool, error)
}

// TasksHandler 处理 /api/admin/tasks 与 /api/admin/tasks/export。
type TasksHandler struct {
	tasks TasksQuerier
	authz Authorizer
	cfg   Config
}

// NewTasksHandler 构造任务检索处理器。Location 与 Clock 仅供后续
// 扩展缺省日口径；业务日的缺省在用例层补齐，判据只此一份。
func NewTasksHandler(tasks TasksQuerier, authz Authorizer, cfg Config) *TasksHandler {
	if cfg.Location == nil {
		cfg.Location = time.UTC
	}
	if cfg.Clock == nil {
		cfg.Clock = coreclock.System{}
	}
	return &TasksHandler{tasks: tasks, authz: authz, cfg: cfg}
}

// List 实现 GET /api/admin/tasks。
func (h *TasksHandler) List(c *gin.Context, params oapi.AdminTaskListParams) {
	scope, _, ok := resolveAdminPrincipal(h.authz, c)
	if !ok {
		return
	}
	filter := tasksFilter(params.Day, stateWord(params.State), params.SchoolId, params.Q)
	if params.Cursor != nil {
		filter.Cursor = string(*params.Cursor)
	}
	// 页大小与用户列表同口径：缺省 50；显式 0 是校验失败，与「没给」区分。
	limit, err := app.PageLimit(params.Limit)
	if err != nil {
		fail(c, err)
		return
	}
	filter.Limit = limit
	page, err := h.tasks.List(c.Request.Context(), scope, filter)
	if err != nil {
		fail(c, err)
		return
	}
	items := make([]oapi.AdminTaskRow, len(page.Items))
	for i, r := range page.Items {
		items[i] = taskRow(r)
	}
	respond.JSON(c.Writer, http.StatusOK, oapi.TaskPage{Items: items, NextCursor: page.NextCursor})
}

// Export 实现 GET /api/admin/tasks/export。
// 截断以响应头显式标记；正文只含被保留的行，不夹说明行。
func (h *TasksHandler) Export(c *gin.Context, params oapi.AdminTaskExportParams) {
	scope, _, ok := resolveAdminPrincipal(h.authz, c)
	if !ok {
		return
	}
	filter := tasksFilter(params.Day, stateWord(params.State), params.SchoolId, params.Q)
	rows, truncated, err := h.tasks.ExportAll(c.Request.Context(), scope, filter)
	if err != nil {
		fail(c, err)
		return
	}
	body, err := tasksCSV(rows)
	if err != nil {
		fail(c, err)
		return
	}
	if truncated {
		c.Writer.Header().Set("X-Export-Truncated", "true")
	}
	c.Writer.Header().Set("Content-Type", "text/csv; charset=utf-8")
	c.Writer.Header().Set("Content-Disposition",
		fmt.Sprintf("attachment; filename=\"tasks-%s.csv\"", filter.Day))
	c.Writer.WriteHeader(http.StatusOK)
	_, _ = c.Writer.Write(body)
}

// tasksFilter 把契约参数折成用例入参。limit 与 cursor 只属于列表端点。
// day 是契约 date 形状：绑定层已解析，这里只取年月日。
func tasksFilter(day *openapi_types.Date, state string, schoolID *openapi_types.UUID, q *string) coreschedule.TaskQueryFilter {
	filter := coreschedule.TaskQueryFilter{}
	if day != nil {
		filter.Day = day.Time.Format("2006-01-02")
	}
	filter.State = state
	if schoolID != nil {
		filter.SchoolID = schoolID.String()
	}
	if q != nil {
		filter.Query = *q
	}
	return filter
}

// stateWord 把契约的可选状态枚举取成字符串。缺省即不过滤。
// 列表与导出是两个具名类型：用泛型收口，不各写一份转换。
func stateWord[T ~string](state *T) string {
	if state == nil {
		return ""
	}
	return string(*state)
}

// taskRow 把用例视图转成传输形状。
func taskRow(r appschedule.TaskView) oapi.AdminTaskRow {
	return oapi.AdminTaskRow{
		Id: r.ID, AccountId: r.AccountID,
		AccountPhoneMasked: r.PhoneMasked,
		SchoolId:           r.SchoolID, SchoolName: r.SchoolName, TaskName: r.TaskName,
		State: r.State, Attempts: r.Attempts, Result: r.Result,
		RunAt: r.RunAt, ClaimedAt: r.ClaimedAt, UpdatedAt: r.UpdatedAt,
	}
}

// csvHeader 是导出的列序，与 AdminTaskRow 字段一一对应。
var csvHeader = []string{"id", "account_id", "account_phone_masked", "school_id",
	"school_name", "task_name", "state", "attempts", "result", "run_at",
	"claimed_at", "updated_at"}

// tasksCSV 按列序渲染 RFC 4180 CSV。时间一律 RFC3339，空值是空单元格。
func tasksCSV(rows []appschedule.TaskView) ([]byte, error) {
	var buf bytes.Buffer
	w := csv.NewWriter(&buf)
	if err := w.Write(csvHeader); err != nil {
		return nil, err
	}
	for _, r := range rows {
		claimed := ""
		if r.ClaimedAt != nil {
			claimed = r.ClaimedAt.Format(time.RFC3339)
		}
		if err := w.Write([]string{
			r.ID, r.AccountID, r.PhoneMasked,
			r.SchoolID, r.SchoolName, r.TaskName,
			r.State, strconv.Itoa(r.Attempts), r.Result,
			r.RunAt.Format(time.RFC3339), claimed, r.UpdatedAt.Format(time.RFC3339),
		}); err != nil {
			return nil, err
		}
	}
	w.Flush()
	if err := w.Error(); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}
