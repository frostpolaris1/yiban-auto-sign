package schedule

// 执行监控端点（UX-5）与全局暂停写面（3a）。
//
// # 功能
// EngineHandler.Snapshot 实现 GET /api/admin/engine：把执行监控用例的
// 快照、执行循环的进程内时刻与全局暂停态拼成一份响应。
// EngineHandler.Pause 实现 PUT /api/admin/engine/pause：写全局暂停开关。
// 本包**不**查库、**不**决定作用域；快照用例在 app/schedule.EngineQuery，
// 暂停读写用例在 app/settings.Service。
//
// # 归属
// entry/http 入口层。作用域由 Authorizer（app/schooladmin）解析，
// 判据与任务检索面同源（resolveAdminPrincipal）。
//
// # 口径
// 只给 master 与 admin；admin 的行与计数经用例按绑定校收窄。
// 进程内三时刻经 EngineStateReader 端口读：本包不 import app/worker，
// 实现由装配注入（与 ManualSigner 的窄端口先例一致）。
// 未启动的循环三时刻为 null：进程时间不从库里补。
// 手机号只有用例给的掩码值；本文件拿不到明文，也不写日志。
// 全局暂停（3a）：快照带 paused（缺行=false）；写面按请求目标值分闸
// ——暂停 master/admin、恢复仅 master（旧线急停先例），二次口令复核
// （缺值 400、错误 403、连错 429 走全局再认证节流）。写入与
// settings.engine_paused 审计同事务由用例层保证；本包不写 SQL。

import (
	"context"
	"errors"
	"math"
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/auth"
	appschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schedule"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
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

// PauseGate 是全局暂停态的读写端口（实现为 app/settings.Service）。
type PauseGate interface {
	EnginePaused(ctx context.Context) (bool, error)
	SetEnginePaused(ctx context.Context, scope corescope.Scope, actor appaudit.Actor, paused bool) error
}

// Reauth 是危险操作二次口令的窄接口（实现在 app/auth.Service）。
// userID 是会话主体；password 是它重新提交的登录口令。
type Reauth interface {
	Reauthenticate(ctx context.Context, userID, password string) error
}

// EngineHandler 处理 /api/admin/engine。
type EngineHandler struct {
	engine EngineQuerier
	state  EngineStateReader
	authz  Authorizer
	pause  PauseGate
	reauth Reauth
}

// NewEngineHandler 构造执行监控处理器。state 不可为 nil：
// 进程时刻是响应的必填面，缺依赖即装配错误。
func NewEngineHandler(engine EngineQuerier, state EngineStateReader, authz Authorizer) *EngineHandler {
	return &EngineHandler{engine: engine, state: state, authz: authz}
}

// WithPauseGate 注入全局暂停读写面（批 3a）。
// 未注入时快照的 paused 恒 false 且 PUT 端点按装配缺失失败关闭。
func (h *EngineHandler) WithPauseGate(gate PauseGate, r Reauth) *EngineHandler {
	h.pause = gate
	h.reauth = r
	return h
}

// Snapshot 实现 GET /api/admin/engine。
func (h *EngineHandler) Snapshot(c *gin.Context) {
	scope, _, ok := resolveAdminPrincipal(h.authz, c)
	if !ok {
		return
	}
	// 全局暂停态（3a）：缺行=false；存储故障上抛（快照发不出去，
	// 也不得把故障伪装成「未暂停」）。未装配时保持 false：该面只缺失于测试装配。
	paused := false
	if h.pause != nil {
		var err error
		if paused, err = h.pause.EnginePaused(c.Request.Context()); err != nil {
			fail(c, err)
			return
		}
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
			TaskId:             r.TaskID,
			AccountId:          r.AccountID,
			AccountPhoneMasked: r.PhoneMasked,
			SchoolId:           r.SchoolID,
			SchoolName:         r.SchoolName,
			TaskName:           r.TaskName,
			Attempts:           r.Attempts,
			RunAt:              r.RunAt,
			Result:             r.Result,
		}
	}
	respond.JSON(c.Writer, http.StatusOK, oapi.EngineSnapshot{
		Inflight:          items,
		DueNow:            int(snap.DueNow),
		TodaySuccess:      int(snap.TodaySuccess),
		TodayFailed:       int(snap.TodayFailed),
		Retrying:          retrying,
		RetryingTruncated: snap.RetryingTruncated,
		Paused:            &paused,
		LastClaimAt:       lastClaim,
		LastTickAt:        lastTick,
		WorkerStartedAt:   started,
	})
}

// Pause 实现 PUT /api/admin/engine/pause（批 3a）。
//
// 顺序有讲究：会话 → 请求体 → 角色门（按请求目标值分闸，恢复仅 master——
// 越权角色不付口令成本）→ 二次口令 → 用例（写与审计同事务）。
// 缺字段 400、缺口令 400、口令错 403（连错走全局再认证节流 429）；
// 被拒路径零写入零审计。
func (h *EngineHandler) Pause(c *gin.Context, _ oapi.AdminEnginePauseParams) {
	scope, user, ok := resolveAdminPrincipal(h.authz, c)
	if !ok {
		return
	}
	// paused 是必填布尔：缺失与 null 都回 400，不能被零值读成「恢复」
	// （对齐 AdminAutoApproveSet 的指针判缺形态）。
	var body struct {
		Paused          *bool  `json:"paused"`
		CurrentPassword string `json:"current_password"`
	}
	if err := c.ShouldBindJSON(&body); err != nil {
		respond.BindError(c.Writer, c.Request, err)
		return
	}
	if body.Paused == nil {
		fail(c, errs.New(errs.CodeValidation, "paused is required"))
		return
	}
	paused := *body.Paused
	// 方向分闸先于二次口令：恢复（paused=false）仅 master，旧线急停先例。
	if !paused && user.Role != coreauth.RoleMaster {
		fail(c, errs.New(errs.CodeForbidden, "master role is required"))
		return
	}
	if !h.reauthenticate(c, user.ID, body.CurrentPassword) {
		return
	}
	if h.pause == nil {
		// 装配缺失是接线错误：失败关闭，不静默当成功。
		fail(c, errs.New(errs.CodeInternal, "pause gate is not wired"))
		return
	}
	if err := h.pause.SetEnginePaused(c.Request.Context(), scope,
		appaudit.NewActor(user), paused); err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, oapi.EnginePauseStatus{Paused: paused})
}

// reauthenticate 是危险操作二次口令的窄入口：缺值 400、错误 403、
// 连续失败 429（Retry-After 由全局再认证节流给出）。
func (h *EngineHandler) reauthenticate(c *gin.Context, userID, password string) bool {
	if h.reauth == nil {
		// 生产装配必须注入；缺失是接线错误，失败关闭。
		fail(c, errs.New(errs.CodeInternal, "reauthentication is not wired"))
		return false
	}
	if password == "" {
		fail(c, errs.New(errs.CodeValidation, "current_password is required"))
		return false
	}
	if err := h.reauth.Reauthenticate(c.Request.Context(), userID, password); err != nil {
		writeReauthRetryAfter(c, err)
		fail(c, err)
		return false
	}
	return true
}

// writeReauthRetryAfter 在节流错误上写 Retry-After 头（秒，向上取整，至少 1）。
func writeReauthRetryAfter(c *gin.Context, err error) {
	var throttled *appauth.ReauthThrottled
	if !errors.As(err, &throttled) {
		return
	}
	seconds := int(math.Ceil(throttled.RetryAfter.Seconds()))
	if seconds < 1 {
		seconds = 1
	}
	c.Header("Retry-After", strconv.Itoa(seconds))
}
