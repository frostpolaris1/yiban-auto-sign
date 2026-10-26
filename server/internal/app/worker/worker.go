package worker

import (
	"context"
	"errors"
	"time"

	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreretry "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/retry"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	corewindow "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/window"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// ErrSignRunning 表示该账号当日任务正在执行。
var ErrSignRunning = coreerrs.New(coreerrs.CodeSignRunning, "sign is already running")

// ErrAlreadySigned 表示该账号当日任务已有结论。
var ErrAlreadySigned = coreerrs.New(coreerrs.CodeAlreadySigned, "already signed")

// ErrWindowClosed 表示窗口已关闭。
var ErrWindowClosed = coreerrs.New(coreerrs.CodeWindowClosed, "window is closed")

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
func (s *Service) Recover(ctx context.Context) (int64, error) {
	return s.deps.Store.ResetRunning(ctx)
}

// Step 认领并执行至多 limit 个到期任务，返回处理数。
// 执行层的结论（成功/失败/跳过）走状态机；基础设施错误原样上抛。
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
	for _, task := range tasks {
		if err := s.execute(ctx, task); err != nil {
			return processed, err
		}
		processed++
	}
	return processed, nil
}

// execute 执行一个已认领任务。返回 error 仅表示基础设施失败。
func (s *Service) execute(ctx context.Context, task corescheduleTask) error {
	windowRecord, err := s.deps.Store.GetWindow(ctx, task.SchoolTaskID, task.Day)
	if err != nil {
		if isNotFound(err) {
			return s.finish(ctx, task, corestatus.TaskSkipped, "window_missing")
		}
		return err
	}
	win := corewindow.Window{Start: windowRecord.Start, End: windowRecord.End}
	now := s.deps.Clock.Now()

	if win.Closed(now) {
		return s.finish(ctx, task, corestatus.TaskSkipped, "window_closed")
	}
	if !win.Open(now) {
		// 提前认领：退回窗口起点，不消耗尝试数以外的判断。
		return s.deps.Store.Requeue(ctx, task.ID, win.Start, "wait_window")
	}

	eg, err := s.deps.Egress.Assign(ctx, task.AccountID)
	if err != nil {
		// 无出口/存储失败：短暂退避，不消耗业务预算。
		return s.deps.Store.Requeue(ctx, task.ID, now.Add(time.Minute), "egress_unavailable")
	}
	if !s.deps.Limiter.Admit(eg.ID, now) {
		wait := s.deps.Limiter.RetryAfter(eg.ID, now)
		if wait <= 0 {
			wait = time.Second
		}
		return s.deps.Store.Requeue(ctx, task.ID, now.Add(wait), "rate_wait")
	}

	cred, err := s.deps.Creds.Credential(ctx, task.AccountID)
	if err != nil {
		return s.finish(ctx, task, corestatus.TaskFailed, "credential_unreadable")
	}
	ref := contract.AccountRef{ID: task.AccountID, SchoolID: task.SchoolID, Platform: "yiban"}

	targets, err := s.deps.Signer.DiscoverTargets(ctx, contract.DiscoverRequest{
		Account: ref, Credential: cred,
	})
	if err != nil {
		return s.settleOutcome(ctx, task, win, now, contract.OutcomeTransient)
	}
	if len(targets) == 0 {
		return s.finish(ctx, task, corestatus.TaskSkipped, string(contract.OutcomeNoTarget))
	}

	result, err := s.deps.Signer.Sign(ctx, contract.SignRequest{
		AttemptID:  task.ID,
		Account:    ref,
		Credential: cred,
		Window:     contract.Window{Start: windowRecord.Start, End: windowRecord.End, Source: contract.WindowPlatform},
		Target:     targets[0],
		Location:   contract.Location{Mode: contract.LocationAuto},
	})
	if err != nil {
		return s.settleOutcome(ctx, task, win, now, contract.OutcomeTransient)
	}
	return s.settleOutcome(ctx, task, win, now, result.Outcome)
}

// settleOutcome 按结论词表落终态或退避重排。
func (s *Service) settleOutcome(ctx context.Context, task corescheduleTask, win corewindow.Window, now time.Time, outcome contract.Outcome) error {
	switch outcome {
	case contract.OutcomeSuccess, contract.OutcomeAlreadyDone:
		return s.finish(ctx, task, corestatus.TaskSuccess, string(outcome))
	case contract.OutcomeNoTarget, contract.OutcomeInvalidTarget,
		contract.OutcomePhotoMissing, contract.OutcomeChallenge,
		contract.OutcomeDeviceUnbound:
		// 有结论、无需重试、且不是"失败"面：当日不再动。
		return s.finish(ctx, task, corestatus.TaskSkipped, string(outcome))
	case contract.OutcomeRejected, contract.OutcomePhotoRejected,
		contract.OutcomeCredentialBad:
		// 确定性失败：留 failed 供告警与人工处置。
		return s.finish(ctx, task, corestatus.TaskFailed, string(outcome))
	}
	// 瞬时/未定/未知：按预算退避；预算耗尽或窗口放不下即失败。
	policy := coreretry.BudgetFor(string(outcome))
	if task.Attempts+1 >= policy.MaxAttempts {
		return s.finish(ctx, task, corestatus.TaskFailed, string(outcome))
	}
	delay, ok := coreretry.NextDelay(policy, 0, coreretry.Window{End: win.End}, now)
	if !ok {
		return s.finish(ctx, task, corestatus.TaskFailed, string(outcome))
	}
	return s.deps.Store.Requeue(ctx, task.ID, now.Add(delay), string(outcome))
}

func (s *Service) finish(ctx context.Context, task corescheduleTask, state corestatus.TaskState, result string) error {
	return s.deps.Store.Finish(ctx, task.AccountID, task.SchoolTaskID, task.Day, state, result, s.deps.Clock.Now())
}

// Request 是手动签到入口：确保当日任务行存在并返回任务 ID。
// 已了结 → already_signed；执行中 → sign_running；关窗 → window_closed。
func (s *Service) Request(ctx context.Context, accountID string) (string, error) {
	if accountID == "" {
		return "", coreerrs.New(coreerrs.CodeValidation, "account is required")
	}
	now := s.deps.Clock.Now()
	day := now.In(s.deps.Location).Format("2006-01-02")
	taskID, err := s.deps.Store.TodayTask(ctx, accountID)
	if err != nil {
		if isNotFound(err) {
			return "", coreerrs.New(coreerrs.CodeValidation, "no enabled task for this account")
		}
		return "", err
	}
	windowRecord, err := s.deps.Store.GetWindow(ctx, taskID, day)
	if err != nil {
		if isNotFound(err) {
			return "", coreerrs.New(coreerrs.CodeValidation, "window is not discovered yet")
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
	}
	return row.ID, nil
}

// corescheduleTask 是 TaskRow 的别名：worker 只依赖这些字段。
type corescheduleTask = coreschedule.TaskRow

func isNotFound(err error) bool {
	return err != nil && errors.Is(err, coreerrs.ErrNotFound)
}
