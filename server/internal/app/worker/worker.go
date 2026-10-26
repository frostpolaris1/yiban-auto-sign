package worker

import (
	"context"
	"errors"
	"fmt"
	"time"

	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
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
)

// Service 是执行循环服务。
type Service struct {
	deps Deps
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
	return &Service{deps: deps}, nil
}

// Recover 回收孤儿 running 行；返回回收数。进程启动时调用一次。
//
// 单进程是明文不变量：启动瞬间仍为 running 的行必然来自上次崩溃，
// 它们不会再有人收尾，必须放回队列。
func (s *Service) Recover(ctx context.Context) (int64, error) {
	return s.deps.Store.ResetRunning(ctx)
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
			if releaseErr := s.deps.Store.RequeueWait(releaseCtx, rest.ID, rest.RunAt, waitBatchAborted); releaseErr != nil {
				// 退回失败优先上报：队列卡死比单次执行失败更严重。
				return processed, releaseErr
			}
		}
		return processed, err
	}
	return processed, nil
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
			return false, s.deps.Store.RequeueWait(ctx, task.ID, s.deps.Clock.Now().Add(windowMissingRetry), waitWindowMissing)
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
		return false, s.deps.Store.RequeueWait(ctx, task.ID, win.Start, waitWindowOpen)
	}

	eg, err := s.deps.Egress.Assign(ctx, task.AccountID)
	if err != nil {
		// 出口面问题（无启用出口/分配失败）不是账号的错：短退避，不计数。
		return false, s.deps.Store.RequeueWait(ctx, task.ID, now.Add(time.Minute), waitEgress)
	}
	if !s.deps.Limiter.Admit(eg.ID, now) {
		wait := s.deps.Limiter.RetryAfter(eg.ID, now)
		if wait <= 0 {
			wait = time.Second // 桶拒绝但等待量为零：兜底 1s，防忙等
		}
		return false, s.deps.Store.RequeueWait(ctx, task.ID, now.Add(wait), waitRate)
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
		if outcome, ok := outcomeOf(err); ok {
			return false, s.settleOutcome(ctx, task, win, now, outcome)
		}
		return false, s.settleOutcome(ctx, task, win, now, contract.OutcomeTransient)
	}
	if len(targets) == 0 {
		return false, s.finish(ctx, task, corestatus.TaskSkipped, string(contract.OutcomeNoTarget))
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
		Target:   targets[0],
		Location: contract.Location{Mode: contract.LocationAuto},
	})
	if err != nil {
		if outcome, ok := outcomeOf(err); ok {
			return true, s.settleOutcome(ctx, task, win, now, outcome)
		}
		return true, s.settleOutcome(ctx, task, win, now, contract.OutcomeTransient)
	}
	return true, s.settleOutcome(ctx, task, win, now, result.Outcome)
}

// settleOutcome 按结论词表落终态或退避重排。
//
// 分档依据（与 core/retry.BudgetFor 一致）：
//   - 成功面：success、already_done → success；
//   - 有结论但不该告警：no_target、invalid_target、photo_missing、
//     challenge、device_unbound → skipped（当日不再动，且不算"失败"）；
//   - 确定性失败：rejected、photo_rejected、credential_invalid → failed；
//   - 可重试面：transient、pending_upstream、unknown → 预算内退避。
func (s *Service) settleOutcome(ctx context.Context, task corescheduleTask, win corewindow.Window, now time.Time, outcome contract.Outcome) error {
	switch outcome {
	case contract.OutcomeSuccess, contract.OutcomeAlreadyDone:
		return s.finish(ctx, task, corestatus.TaskSuccess, string(outcome))
	case contract.OutcomeNoTarget, contract.OutcomeInvalidTarget,
		contract.OutcomePhotoMissing, contract.OutcomeChallenge,
		contract.OutcomeDeviceUnbound:
		return s.finish(ctx, task, corestatus.TaskSkipped, string(outcome))
	case contract.OutcomeRejected, contract.OutcomePhotoRejected,
		contract.OutcomeCredentialBad:
		return s.finish(ctx, task, corestatus.TaskFailed, string(outcome))
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
	return s.deps.Store.RequeueRetry(ctx, task.ID, now.Add(delay), string(outcome))
}

// finish 落终态。终态只认 core/status 的三值。
func (s *Service) finish(ctx context.Context, task corescheduleTask, state corestatus.TaskState, result string) error {
	return s.deps.Store.Finish(ctx, task.AccountID, task.SchoolTaskID, task.Day, state, result, s.deps.Clock.Now())
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

// outcomeOf 从契约错误里取业务结论；不是 OutcomeError 返回 false。
func outcomeOf(err error) (contract.Outcome, bool) {
	var oe *contract.OutcomeError
	if errors.As(err, &oe) && oe.Outcome != "" {
		return oe.Outcome, true
	}
	return "", false
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
