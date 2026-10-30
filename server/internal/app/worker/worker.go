package worker

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	appcore "github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appevents "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/events"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corelocation "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/location"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
	coreretry "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/retry"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	corewindow "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/window"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// 三个手动签到的结论错误。
// 用包级哨兵而不是每次新建：入口层按 errors.Is 判定，错误码稳定。
var (
	// ErrSignRunning 表示该账号当日任务正在执行。
	ErrSignRunning = coreerrs.New(coreerrs.CodeSignRunning, "sign is already running")
	// ErrAlreadySigned 表示该账号当日任务已有结论。
	ErrAlreadySigned = coreerrs.New(coreerrs.CodeAlreadySigned, "already signed")
	// ErrWindowClosed 表示窗口已关闭。
	ErrWindowClosed = coreerrs.New(coreerrs.CodeWindowClosed, "window is closed")
)

// 内部等待原因（写入 sign_tasks.result）。
//
// 这些词**不是** signing 结论词：它们描述"还没真正试过"的原因，
// 入口层与统计不得把 wait_* 当作业务结论。真正的业务结论一律取
// contract.Outcome 的词面值。前缀 wait_ 就是给读库的人一个直观分界。
const (
	waitWindowOpen     = "wait_window_open"    // 窗口未开（已知窗口，退回起点）
	waitWindowMissing  = "wait_window_missing" // 当日窗口尚未发现（定时再看）
	waitRate           = "wait_rate"           // 出口限速未放行
	waitEgress         = "wait_egress"         // 出口不可用（无可用出口或分配失败）
	waitBatchAborted   = "wait_batch_aborted"  // 同批前一条基础设施失败，本行退回队列
	unreadableCred     = "credential_invalid"
	windowMissingRetry = 10 * time.Minute // 窗口未发现时的重试间隔
	// reclaimBatch 是每 tick 回收过期租约的上限。
	reclaimBatch = 100
)

// locationOutOfRange 是宿主自产的确定性结论词（与 window_closed 同形）：
// 固定点落在目标围栏外且无原因说明。它不在 contract.Outcome 词表内——
// 提供方永不返回它；确定结论不退避，落 skipped 档。
const locationOutOfRange = "location_out_of_range" // 与 core/errs.CodeLocationOutside 同词：宿主自产结论，不经 contract.Outcome。

// Service 是执行循环服务。
type Service struct {
	deps Deps
	log  *slog.Logger
}

// NewService 构造服务。缺依赖即失败。
func NewService(deps Deps) (*Service, error) {
	if deps.Store == nil {
		return nil, errors.New("worker: nil store")
	}
	if deps.Creds == nil {
		return nil, errors.New("worker: nil credentials")
	}
	if deps.Signer == nil {
		return nil, errors.New("worker: nil signer")
	}
	if deps.Egress == nil {
		return nil, errors.New("worker: nil egress")
	}
	if deps.Limiter == nil {
		return nil, errors.New("worker: nil limiter")
	}
	if deps.Clock == nil {
		return nil, errors.New("worker: nil clock")
	}
	if deps.Location == nil {
		return nil, errors.New("worker: nil location")
	}
	log := deps.Logger
	if log == nil {
		log = slog.Default()
	}
	return &Service{deps: deps, log: log}, nil
}

// Recover 回收无租约或租约已过期的 running 行；返回回收数。进程启动时调用一次。
//
// 单进程是明文不变量：启动瞬间仍为 running 的行必然来自上次崩溃，
// 它们不会再有人收尾，必须放回队列。
func (s *Service) Recover(ctx context.Context) (int64, error) {
	return s.deps.Store.ResetRunning(ctx)
}

// CountRunning 数当前 running 行。优雅停机用它报在飞清零。
func (s *Service) CountRunning(ctx context.Context) (int64, error) {
	return s.deps.Store.CountRunning(ctx)
}

// Step 认领并执行至多 limit 个到期任务，返回处理数。
//
// 两类失败要分清：
//   - 业务结论（成功/跳过/失败）由 settleOutcome 落状态，Step 返回 nil；
//   - 基础设施失败（库、网络、取消）原样上抛，且**同批已认领未执行的行
//     要退回队列**：否则它们停在 running 上，该账号当天再也领不到新任务
//     （P4-E 审查 P2-2）。
func (s *Service) Step(ctx context.Context, limit int) (int, error) {
	if limit < 1 {
		limit = 1
	}
	now := s.deps.Clock.Now()
	// 先回收租约过期的行：崩溃或收尾失败留下的 running 不得跨 tick 卡住账号。
	if reclaimed, err := s.deps.Store.ReclaimExpired(ctx, now, reclaimBatch); err != nil {
		// 运行期必须可见：回收失败会让认领永久不可达，进程却看着健康。
		s.log.Error("worker: reclaim expired leases failed", "error", err.Error())
		return 0, err
	} else if reclaimed > 0 {
		s.log.Warn("worker: reclaimed expired task leases", "count", reclaimed)
	}
	tasks, err := s.deps.Store.ClaimDue(ctx, now, limit)
	if err != nil {
		return 0, err
	}
	processed := 0
	for i, task := range tasks {
		submitted, err := s.execute(ctx, task)
		if err == nil {
			processed++
			continue
		}
		// 失败分两种，退回范围不同：
		//   - 尚未提交签到（窗口/凭据/发现等前置步骤失败）：当前行也没产生
		//     外部副作用，连它一起退回队列；
		//   - 已提交签到后才失败（落库/收尾失败）：当前行可能已在平台侧生效，
		//     退回会重复签到，故只退后面的行，当前行留在 running 由重启回收。
		releaseFrom := i + 1
		if !submitted {
			releaseFrom = i
		}
		// 退回用 WithoutCancel：即使 ctx 已取消，队列也必须被修好，
		// 否则这些行停在 running 到重启（P4-E 复审 P3-1）。
		releaseCtx := context.WithoutCancel(ctx)
		for _, rest := range tasks[releaseFrom:] {
			if releaseErr := s.requeueWait(releaseCtx, rest, rest.RunAt, waitBatchAborted); releaseErr != nil {
				// 退回失败优先上报：队列卡死比单次执行失败更严重。
				return processed, releaseErr
			}
		}
		s.logSettlementError(err)
		return processed, err
	}
	return processed, nil
}

// logSettlementError 让栅栏拒绝在运行期可见。
// 冲突表示本次认领已被回收或接线丢失；只记这一类，避免每 tick 刷日志。
func (s *Service) logSettlementError(err error) {
	if err == nil {
		return
	}
	if coreerrs.From(err).Code == coreerrs.CodeConflict {
		s.log.Error("worker: settlement rejected by the claim fence", "error", err.Error())
	}
}

// execute 执行一个已认领任务。返回 error 仅表示基础设施失败。
//
// 顺序不是随意的：
//  1. 窗口判定最先——关窗后一个请求都不该发；
//  2. 出口与限速在凭据之前——拿不到额度就不必解密口令；
//  3. 凭据、发现、提交构成一次真实尝试，结论交给 settleOutcome。
func (s *Service) execute(ctx context.Context, task corescheduleTask) (bool, error) {
	windowRecord, err := s.deps.Store.GetWindow(ctx, task.SchoolTaskID, task.Day)
	if err != nil {
		if isNotFound(err) {
			// 窗口尚未发现不是结论：10 分钟后再看。若当日始终没有窗口，
			// 行会保持 pending 到下一个业务日，由日切逻辑（P4-F）清理。
			return false, s.requeueWait(ctx, task, s.deps.Clock.Now().Add(windowMissingRetry), waitWindowMissing)
		}
		return false, err
	}
	win := corewindow.Window{Start: windowRecord.Start, End: windowRecord.End}
	now := s.deps.Clock.Now()

	if win.Closed(now) {
		// 关窗落终态：skipped 表示"有结论、今日不再动"，与 failed 区分。
		return false, s.finish(ctx, task, corestatus.TaskSkipped, "window_closed")
	}
	if !win.Open(now) {
		// 提前认领：退回窗口起点。等待不计业务尝试数。
		return false, s.requeueWait(ctx, task, win.Start, waitWindowOpen)
	}

	eg, err := s.deps.Egress.Assign(ctx, task.AccountID)
	if err != nil {
		// 出口面问题（无启用出口/分配失败）不是账号的错：短退避，不计数。
		return false, s.requeueWait(ctx, task, now.Add(time.Minute), waitEgress)
	}
	if !s.deps.Limiter.Admit(eg.ID, now) {
		wait := s.deps.Limiter.RetryAfter(eg.ID, now)
		if wait <= 0 {
			wait = time.Second // 桶拒绝但等待量为零：兜底 1s，防忙等
		}
		return false, s.requeueWait(ctx, task, now.Add(wait), waitRate)
	}

	cred, err := s.deps.Creds.Credential(ctx, task.AccountID)
	if err != nil {
		// 分类处理，别把一次库抖动写成当日业务失败（P4-E 审查 P2-3）：
		//   - 取消/超时：上抛，由上层决定整批重来；
		//   - 账号不可用（not_found/validation/forbidden/unauthorized）：
		//     当日确定性失败，落 failed；
		//   - 其余（DB、KEK 等）：基础设施错误，上抛。
		if ctx.Err() != nil || errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
			return false, err
		}
		switch coreerrs.From(err).Code {
		case coreerrs.CodeNotFound, coreerrs.CodeValidation,
			coreerrs.CodeForbidden, coreerrs.CodeUnauthorized:
			return false, s.finish(ctx, task, corestatus.TaskFailed, unreadableCred)
		default:
			return false, err
		}
	}
	ref := contract.AccountRef{ID: task.AccountID, SchoolID: task.SchoolID, Platform: "yiban"}

	// 发现阶段可能直接给出业务结论（OutcomeError），例如 no_target、
	// credential_invalid、challenge。契约要求宿主用 errors.As 取回；
	// 把它当 transient 重试会让 challenge 被多打三次真实请求（P4-E 审查 P1-1）。
	targets, err := s.deps.Signer.DiscoverTargets(ctx, contract.DiscoverRequest{
		Account: ref, Credential: cred,
	})
	if err != nil {
		if outcome, reason, ok := outcomeOf(err); ok {
			s.logOutcome(ctx, task, stageDiscovery, outcome, reason)
			return false, s.settleOutcome(ctx, task, win, now, outcome)
		}
		s.logOutcome(ctx, task, stageDiscovery, contract.OutcomeTransient, mask.SafeErrorText(err))
		return false, s.settleOutcome(ctx, task, win, now, contract.OutcomeTransient)
	}
	if len(targets) == 0 {
		return false, s.finish(ctx, task, corestatus.TaskSkipped, string(contract.OutcomeNoTarget))
	}
	// 定位先于选点：固定点可能只落在部分候选围栏内。
	location := contract.Location{Mode: contract.LocationAuto}
	if s.deps.Locations != nil {
		location, err = s.deps.Locations.Resolve(ctx, task.AccountID)
		if err != nil {
			// 定位读取是库面依赖：失败不是账号结论，上抛退回队列。
			return false, err
		}
	}

	// 候选点按尝试序号轮转，并跳过固定点落不到的围栏。
	// 全部候选都不可用是一次确定性的当日结论，不发起登录、不消耗尝试数。
	target, ok := pickTarget(targets, task.Attempts, location)
	if !ok {
		return false, s.finish(ctx, task, corestatus.TaskSkipped, locationOutOfRange)
	}

	// auto 定位在围栏内取确定点：种子取本次尝试的幂等键，
	// 故同一尝试恒得同一点。采样不成立（无多边形/退化/用尽投点）时
	// 保持零坐标，提供方回退上游中心点。固定点由用户指定，不参与采样。
	if location.Mode != contract.LocationFixed {
		if point, ok := corelocation.SamplePoint(target.Polygon, attemptID(task)); ok {
			location.Lng, location.Lat = point.Lng, point.Lat
		}
	}

	// 图片注入：要求图片的目标必须拿到图。库空（picked=false）与
	// 端口未装配同等看待——不带图的提交注定失败，宁可当日跳过。
	// 属主取自杀到行：选图按账号所有者的图片库轮换。
	var image *contract.ImageRef
	if target.RequiresPhoto {
		picked := false
		var ref contract.ImageRef
		if s.deps.Photos != nil {
			var err error
			ref, picked, err = s.deps.Photos.PickForAttempt(ctx, task.OwnerUserID, attemptID(task))
			if err != nil {
				// 选图读库或 blob 失败：基础设施错误，上抛退回队列。
				return false, err
			}
		}
		if !picked {
			return false, s.finish(ctx, task, corestatus.TaskSkipped, string(contract.OutcomePhotoMissing))
		}
		image = &ref
	}

	result, err := s.deps.Signer.Sign(ctx, contract.SignRequest{
		AttemptID:  attemptID(task),
		Account:    ref,
		Credential: cred,
		Window: contract.Window{
			Start: windowRecord.Start, End: windowRecord.End,
			// 来源如实传递：fallback 窗口不得伪装成 platform。
			// 空来源只可能来自脏数据；兜底为 fallback 而不是 platform，
			// 让证据面显示"这不是实拍窗口"（P4-E 复审 P3-5）。
			Source: windowSource(windowRecord.Source),
		},
		Target:   target,
		Location: location,
		Image:    image,
	})
	if err != nil {
		if outcome, reason, ok := outcomeOf(err); ok {
			s.logOutcome(ctx, task, stageSubmit, outcome, reason)
			return true, s.settleOutcome(ctx, task, win, now, outcome)
		}
		s.logOutcome(ctx, task, stageSubmit, contract.OutcomeTransient, mask.SafeErrorText(err))
		return true, s.settleOutcome(ctx, task, win, now, contract.OutcomeTransient)
	}
	s.logOutcome(ctx, task, stageSubmit, result.Outcome, result.Message)
	return true, s.settleOutcome(ctx, task, win, now, result.Outcome)
}

// settleOutcome 按结论词表落终态或退避重排。
//
// 分档依据（与 core/retry.BudgetFor 一致）：
//   - 成功面：success、already_done → success；
//   - 有结论但不该告警：no_target、invalid_target、photo_missing、
//     challenge、device_unbound → skipped（当日不再动，且不算"失败"）；
//   - 确定性失败：rejected、photo_rejected → failed；
//   - 凭据错：credential_invalid → failed，并把账号移出排期；
//   - 可重试面：transient、pending_upstream、unknown → 预算内退避。
func (s *Service) settleOutcome(ctx context.Context, task corescheduleTask, win corewindow.Window, now time.Time, outcome contract.Outcome) error {
	switch outcome {
	case contract.OutcomeSuccess, contract.OutcomeAlreadyDone:
		return s.finish(ctx, task, corestatus.TaskSuccess, string(outcome))
	case contract.OutcomeNoTarget, contract.OutcomeInvalidTarget,
		contract.OutcomePhotoMissing, contract.OutcomeChallenge,
		contract.OutcomeDeviceUnbound:
		return s.finish(ctx, task, corestatus.TaskSkipped, string(outcome))
	case contract.OutcomeRejected, contract.OutcomePhotoRejected:
		return s.finish(ctx, task, corestatus.TaskFailed, string(outcome))
	case contract.OutcomeCredentialBad:
		return s.finishCredentialInvalid(ctx, task)
	}
	// 重试面：预算耗尽或窗口放不下即落 failed。
	// NextDelay 的 last 固定为 0：退避从基础间隔起步，抖动交给后续批次
	// （当前是确定性的 base 间隔，便于复算与测试）。
	policy := coreretry.BudgetFor(string(outcome))
	if task.Attempts+1 >= policy.MaxAttempts {
		return s.finish(ctx, task, corestatus.TaskFailed, string(outcome))
	}
	delay, ok := coreretry.NextDelay(policy, 0, coreretry.Window{End: win.End}, now)
	if !ok {
		return s.finish(ctx, task, corestatus.TaskFailed, string(outcome))
	}
	// 业务失败重排要计尝试数（与等待型 RequeueWait 区分）。
	return s.requeueRetry(ctx, task, now.Add(delay), string(outcome))
}

// requeueWait 是等待型重排：成功后发 task.progress。
// attempt 口径 = 落库后的 sign_tasks.attempts；等待不计尝试。
func (s *Service) requeueWait(ctx context.Context, task corescheduleTask, runAt time.Time, result string) error {
	if err := s.deps.Store.RequeueWait(ctx, task.ID, runAt, result, task.ClaimSeq); err != nil {
		return err
	}
	s.notifyProgress(ctx, task, task.Attempts)
	return nil
}

// requeueRetry 是业务退避重排：成功后发 task.progress。
// RequeueRetry 计一次尝试，故 attempt 为已消耗数加一。
func (s *Service) requeueRetry(ctx context.Context, task corescheduleTask, runAt time.Time, result string) error {
	if err := s.deps.Store.RequeueRetry(ctx, task.ID, runAt, result, task.ClaimSeq); err != nil {
		return err
	}
	s.notifyProgress(ctx, task, task.Attempts+1)
	return nil
}

// notifyProgress 发布 task.progress。payload 只放 account_id 与 attempt。
// 发布不得阻塞或改变结算语义：实现失败只丢事件，panic 就地吞掉。
func (s *Service) notifyProgress(ctx context.Context, task corescheduleTask, attempt int) {
	if s.deps.Notifier == nil {
		return
	}
	defer func() { _ = recover() }() // 发布失败只丢事件
	s.deps.Notifier.TaskEvent(ctx, task.SchoolID, task.SchoolTaskID, task.Day,
		appevents.TypeTaskProgress, map[string]any{"account_id": task.AccountID, "attempt": attempt})
}

// finish 落终态。终态只认 core/status 的三值。
// 落库成功后发 task.concluded（payload：account_id、state、attempt）。
func (s *Service) finish(ctx context.Context, task corescheduleTask, state corestatus.TaskState, result string) error {
	if err := s.deps.Store.Finish(ctx, task.AccountID, task.SchoolTaskID, task.Day, state, result, task.ClaimSeq, s.deps.Clock.Now()); err != nil {
		return err
	}
	s.notifyConcluded(ctx, task, state)
	return nil
}

// finishCredentialInvalid 落 failed(credential_invalid)，并由存储层
// 在同一事务里把账号移出排期。落库成功后同样发 task.concluded。
//
// 为什么不能复用 finish：账号状态是排期输入。只落任务不落账号，
// 次日仍会认领同一账号并再打一次失败登录（验收 3）。
func (s *Service) finishCredentialInvalid(ctx context.Context, task corescheduleTask) error {
	if err := s.deps.Store.FinishCredentialInvalid(ctx, task.SchoolID, task.AccountID, task.SchoolTaskID, task.Day, task.ClaimSeq, s.deps.Clock.Now()); err != nil {
		return err
	}
	s.notifyConcluded(ctx, task, corestatus.TaskFailed)
	return nil
}

// notifyConcluded 发布 task.concluded。
// attempt 口径 = 落库后的 sign_tasks.attempts：两条落库路径都计一次尝试。
// 发布不得阻塞或改变结算语义：实现失败只丢事件，panic 就地吞掉。
func (s *Service) notifyConcluded(ctx context.Context, task corescheduleTask, state corestatus.TaskState) {
	if s.deps.Notifier == nil {
		return
	}
	defer func() { _ = recover() }() // 发布失败只丢事件
	s.deps.Notifier.TaskEvent(ctx, task.SchoolID, task.SchoolTaskID, task.Day,
		appevents.TypeTaskConcluded, map[string]any{
			"account_id": task.AccountID,
			"state":      string(state),
			"attempt":    task.Attempts + 1,
		})
}

// pickTarget 选出本次尝试的候选点。
//
// 起点按尝试序号轮转：第 1 次尝试取第 1 个，第 2 次取第 2 个，用完回到开头。
// 轮转不改变总尝试次数——尝试数由重试预算决定。
// 固定定位落在围栏外且无原因说明时，该候选不可用：从起点向后找第一个可用的。
// 全部不可用返回 false，由调用方落 location_out_of_range。
// 带原因说明的固定点不做围栏过滤（既有放行语义），按序号纯轮转。
// 负数只可能来自脏数据：回落到第一个候选，不 panic。
func pickTarget(targets []contract.Target, attempts int, loc contract.Location) (contract.Target, bool) {
	if len(targets) == 0 {
		return contract.Target{}, false
	}
	start := attempts % len(targets)
	if start < 0 {
		start = 0
	}
	if loc.Reason != "" {
		return targets[start], true
	}
	for i := 0; i < len(targets); i++ {
		candidate := targets[(start+i)%len(targets)]
		if corelocation.Resolve(candidate, loc) {
			return candidate, true
		}
	}
	return contract.Target{}, false
}

// attemptID 生成"按尝试唯一"的幂等键。
//
// 为什么不能只用 task.ID：provider 按 (账号, AttemptID) 缓存结果（含
// transient）。若每次重试都送同一个 ID，"重试"只会读回首次结果、不发请求
// （P4-E 审查 P1-2）。带上 attempts 后，每次真实尝试都有新键；
// 同一尝试被重复调用时仍折叠（这正是幂等缓存要防的双发）。
func attemptID(task corescheduleTask) string {
	return fmt.Sprintf("%s#%d", task.ID, task.Attempts)
}

// windowSource 归一窗口来源；空值兜底为 fallback（保守方向）。
func windowSource(raw string) contract.WindowSource {
	if raw == "" {
		return contract.WindowFallback
	}
	return contract.WindowSource(raw)
}

// outcomeOf 从契约错误里取业务结论与原因；不是 OutcomeError 返回 false。
// 原因进日志：sign_tasks.result 只存结论词，词表不得扩展。
func outcomeOf(err error) (contract.Outcome, string, bool) {
	var oe *contract.OutcomeError
	if errors.As(err, &oe) && oe.Outcome != "" {
		return oe.Outcome, oe.Message, true
	}
	return "", "", false
}

// 结论来源：区分发现面与提交面。
// 无 message 的结论靠 stage 定位来源（OCR L1）。
const (
	stageDiscovery = "discovery"
	stageSubmit    = "submit"
)

// logOutcome 记录一次结论的原因。
//
// result 列只存结论词（词表不许扩展），提供方给的 message 是唯一详情。
// 故每条结论都落一行结构化日志；unknown 即使没有原因也记，
// 让"提供方没给原因"这个缺口本身可见。
// 级别取 app.OutcomeLogLevel：正常结论 Info，需人处置的 Warn。
// reason 先过 mask.MaskText：日志与审计共用同一套掩码口径。
func (s *Service) logOutcome(ctx context.Context, task corescheduleTask, stage string, outcome contract.Outcome, reason string) {
	if reason == "" && outcome != contract.OutcomeUnknown {
		return
	}
	s.log.Log(ctx, appcore.OutcomeLogLevel(outcome), "signing outcome",
		"stage", stage,
		"school_id", task.SchoolID,
		"account_id", task.AccountID,
		"task_id", task.SchoolTaskID,
		"day", task.Day,
		"attempt", task.Attempts+1,
		"outcome", string(outcome),
		"reason", mask.MaskText(reason),
	)
}

// Request 是手动签到入口：确保当日任务行存在并返回任务 ID。
//
// 结论语义（与入口层错误码一一对应）：
//   - 关窗 → ErrWindowClosed；
//   - 已成功/已跳过 → ErrAlreadySigned；
//   - 正在执行 → ErrSignRunning；
//   - 当日 failed → 重新放回队列（Rearm）并返回任务 ID，这是用户
//     "再试一次"的正当动作（P4-E 审查 P2-4）；
//   - 其余（新建或已 pending）→ 返回任务 ID（幂等）。
func (s *Service) Request(ctx context.Context, accountID string) (string, error) {
	if accountID == "" {
		return "", coreerrs.New(coreerrs.CodeValidation, "account is required")
	}
	now := s.deps.Clock.Now()
	day := now.In(s.deps.Location).Format("2006-01-02")
	taskID, err := s.deps.Store.TodayTask(ctx, accountID)
	if err != nil {
		if isNotFound(err) {
			// 账号不可用或学校没有启用任务：是资源不存在，不是参数格式错。
			return "", coreerrs.New(coreerrs.CodeNotFound, "no enabled task for this account")
		}
		return "", err
	}
	windowRecord, err := s.deps.Store.GetWindow(ctx, taskID, day)
	if err != nil {
		if isNotFound(err) {
			return "", coreerrs.New(coreerrs.CodeNotFound, "window is not discovered yet")
		}
		return "", err
	}
	win := corewindow.Window{Start: windowRecord.Start, End: windowRecord.End}
	if win.Closed(now) {
		return "", ErrWindowClosed
	}
	row, _, err := s.deps.Store.EnsureTask(ctx, accountID, taskID, day, now)
	if err != nil {
		return "", err
	}
	switch corestatus.TaskState(row.State) {
	case corestatus.TaskRunning:
		return "", ErrSignRunning
	case corestatus.TaskSuccess, corestatus.TaskSkipped:
		return "", ErrAlreadySigned
	case corestatus.TaskFailed:
		if err := s.deps.Store.Rearm(ctx, row.ID, now); err != nil {
			return "", err
		}
		return row.ID, nil
	}
	return row.ID, nil
}

// corescheduleTask 是 TaskRow 的别名：worker 只依赖这些字段。
type corescheduleTask = coreschedule.TaskRow

func isNotFound(err error) bool {
	return err != nil && errors.Is(err, coreerrs.ErrNotFound)
}
