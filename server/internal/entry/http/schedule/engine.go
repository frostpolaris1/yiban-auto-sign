package schedule

// 执行监控快照端点（UX-5）。
//
// # 功能
// EngineHandler.Snapshot 实现 GET /api/admin/engine：把执行监控用例的
// 快照与执行循环的进程内时刻拼成一份响应。
// 本包**不**查库、**不**决定作用域；快照用例在 app/schedule.EngineQuery。
//
// # 归属
// entry/http 入口层。作用域由 Authorizer（app/admin）解析，
// 判据与任务检索面同源（resolveAdminPrincipal）。
//
// # 口径
// 只给 master 与 admin；admin 的行与计数经用例按绑定校收窄。
// 进程内三时刻经 EngineStateReader 端口读：本包不 import app/worker，
// 实现由装配注入（与 ManualSigner 的窄端口先例一致）。
// 未启动的循环三时刻为 null：进程时间不从库里补。
// 手机号只有用例给的掩码值；本文件拿不到明文，也不写日志。

import (
	"context"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"

	appschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
)

// EngineQuerier 是执行监控快照用例端口（实现为 *appschedule.EngineQuery）。
type EngineQuerier interface {
	Snapshot(ctx context.Context, scope corescope.Scope) (appschedule.EngineSnapshot, error)
}

// EngineStateReader 是执行循环进程时刻的只读端口
// （实现为 app/worker.EngineState）。未发生的时刻为 nil。
type EngineStateReader interface {
	EngineTimes() (startedAt, lastTickAt, lastClaimAt *time.Time)
}

// EngineHandler 处理 /api/admin/engine。
type EngineHandler struct {
	engine EngineQuerier
	state  EngineStateReader
	authz  Authorizer
}

// NewEngineHandler 构造执行监控处理器。state 不可为 nil：
// 进程时刻是响应的必填面，缺依赖即装配错误。
func NewEngineHandler(engine EngineQuerier, state EngineStateReader, authz Authorizer) *EngineHandler {
	return &EngineHandler{engine: engine, state: state, authz: authz}
}

// Snapshot 实现 GET /api/admin/engine。
func (h *EngineHandler) Snapshot(c *gin.Context) {
	scope, _, ok := resolveAdminPrincipal(h.authz, c)
	if !ok {
		return
	}
	snap, err := h.engine.Snapshot(c.Request.Context(), scope)
	if err != nil {
		fail(c, err)
		return
	}
	started, lastTick, lastClaim := h.state.EngineTimes()
	items := make([]oapi.EngineInflightRow, len(snap.Inflight))
	for i, r := range snap.Inflight {
		items[i] = oapi.EngineInflightRow{
			AccountId:          r.AccountID,
			AccountPhoneMasked: r.PhoneMasked,
			SchoolId:           r.SchoolID,
			SchoolName:         r.SchoolName,
			ClaimedAt:          r.ClaimedAt,
			LeaseExpiresAt:     r.LeaseExpiresAt,
		}
	}
	retrying := make([]oapi.EngineRetryRow, len(snap.Retrying))
	for i, r := range snap.Retrying {
		retrying[i] = oapi.EngineRetryRow{
			AccountId:          r.AccountID,
			AccountPhoneMasked: r.PhoneMasked,
			SchoolId:           r.SchoolID,
			SchoolName:         r.SchoolName,
			Attempts:           r.Attempts,
			RunAt:              r.RunAt,
			Result:             r.Result,
		}
	}
	respond.JSON(c.Writer, http.StatusOK, oapi.EngineSnapshot{
		Inflight:        items,
		DueNow:          int(snap.DueNow),
		TodaySuccess:    int(snap.TodaySuccess),
		TodayFailed:     int(snap.TodayFailed),
		Retrying:        retrying,
		LastClaimAt:     lastClaim,
		LastTickAt:      lastTick,
		WorkerStartedAt: started,
	})
}
