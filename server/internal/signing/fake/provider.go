// Package fake 提供只供测试使用的假 Provider。
//
// # 功能
// 按脚本返回窗口、目标、身份与签到结果。
// 每个方法可以预设响应序列、延迟与错误。
// 同一个 AttemptID 的重复签到返回首次结果。
// 本包**不**连网络、**不**读配置、**不**使用随机数。
//
// # 归属
// signing 层的测试替身。
// 各 provider 的单元测试与 conformance 判据都用它。
// **生产代码禁止导入本包**：门规则 fake.import 把生产导入判红。
//
// # 复用
// Provider、Step、New。
// ValidWindow、ValidTargets、ValidProfile。
// AllOutcomes、ResultFor、ResultSteps、OutcomeAdvice、FixedTime。
//
// # 口径
// 第 N 次调用取脚本第 N 项；序列用尽后重复最后一项。
// 异构脚本按**调用到达顺序**发牌，不绑定 AttemptID：
// 并发用例不得依赖发牌顺序，改用 OnAttempt 或 Expecter 固定每项。
// 未配置脚本的方法返回零值与 nil 错误；
// 例外：未声明能力的发现方法返回非 nil 错误（契约 G1）。
// 被上下文取消的调用仍消耗一项脚本。
// 声明 ImageRequired 时，无图请求返回 photo_missing，且**不消耗脚本**。
// 空 AttemptID 关闭幂等缓存。
// **全部时间取 FixedTime**，本包不读墙上时钟。
package fake

import (
	"context"
	"errors"
	"sync"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// FixedTime 是脚本与证据使用的唯一时间点。
var FixedTime = time.Date(2026, 10, 26, 6, 0, 0, 0, time.UTC)

// Step 是一次调用的预设响应。
// 先等待 Delay，再返回 Err 或 Value。
type Step[T any] struct {
	Value T
	Err   error
	Delay time.Duration
}

// Provider 是可编程的假提供方。
// **配置必须在并发使用前完成**；并发阶段只读配置。
type Provider struct {
	// Expect 可选。设置后 ExpectedOutcome 先询问它。
	Expect func(contract.SignRequest) (contract.Outcome, bool)

	id   string
	caps contract.Capabilities

	windowSteps  []Step[contract.Window]
	targetSteps  []Step[[]contract.Target]
	profileSteps []Step[contract.Profile]
	signSteps    []Step[contract.Result]
	attemptFixed map[string]contract.Result

	mu        sync.Mutex
	windowN   int
	targetN   int
	profileN  int
	signN     int
	windowHit int
	targetHit int
	verifyHit int
	signHit   int
	attempts  map[string]contract.Result
	locks     map[string]*sync.Mutex
}

var _ contract.Provider = (*Provider)(nil)

// New 构造假 Provider。
// id 与 caps 原样保存；判据负责判定它们是否合格。
func New(id string, caps contract.Capabilities) *Provider {
	return &Provider{
		id:           id,
		caps:         caps,
		attemptFixed: make(map[string]contract.Result),
		attempts:     make(map[string]contract.Result),
		locks:        make(map[string]*sync.Mutex),
	}
}

// OnWindow 追加窗口发现脚本。
func (p *Provider) OnWindow(steps ...Step[contract.Window]) *Provider {
	p.windowSteps = append(p.windowSteps, steps...)
	return p
}

// OnTargets 追加目标发现脚本。
func (p *Provider) OnTargets(steps ...Step[[]contract.Target]) *Provider {
	p.targetSteps = append(p.targetSteps, steps...)
	return p
}

// OnVerify 追加凭据校验脚本。
func (p *Provider) OnVerify(steps ...Step[contract.Profile]) *Provider {
	p.profileSteps = append(p.profileSteps, steps...)
	return p
}

// OnSign 追加签到脚本。
func (p *Provider) OnSign(steps ...Step[contract.Result]) *Provider {
	p.signSteps = append(p.signSteps, steps...)
	return p
}

// OnAttempt 为一个 AttemptID 固定结果。
// 它优先于 OnSign 的序列，重复调用返回同一结果。
func (p *Provider) OnAttempt(attemptID string, result contract.Result) *Provider {
	p.attemptFixed[attemptID] = result
	return p
}

// ID 返回构造时给定的标识。
func (p *Provider) ID() string { return p.id }

// Capabilities 返回构造时给定的能力。
func (p *Provider) Capabilities() contract.Capabilities { return p.caps }

// DiscoverWindow 返回下一项窗口脚本。
func (p *Provider) DiscoverWindow(ctx context.Context, _ contract.DiscoverRequest) (contract.Window, error) {
	// G1：未声明能力的发现方法必须返回非 nil 错误（默认行为，可被脚本覆盖）。
	if !p.caps.WindowDiscovery {
		return contract.Window{}, errors.New("fake: WindowDiscovery not declared")
	}
	p.count(&p.windowHit)

	p.mu.Lock()
	step := next(p.windowSteps, &p.windowN)
	p.mu.Unlock()

	if err := wait(ctx, step.Delay); err != nil {
		return contract.Window{}, err
	}
	if step.Err != nil {
		return contract.Window{}, step.Err
	}
	return step.Value, nil
}

// DiscoverTargets 返回下一项目标脚本。
func (p *Provider) DiscoverTargets(ctx context.Context, _ contract.DiscoverRequest) ([]contract.Target, error) {
	if !p.caps.TargetDiscovery {
		return nil, errors.New("fake: TargetDiscovery not declared")
	}
	p.count(&p.targetHit)

	p.mu.Lock()
	step := next(p.targetSteps, &p.targetN)
	p.mu.Unlock()

	if err := wait(ctx, step.Delay); err != nil {
		return nil, err
	}
	if step.Err != nil {
		return nil, step.Err
	}
	return step.Value, nil
}

// Verify 返回下一项身份脚本。
func (p *Provider) Verify(ctx context.Context, _ contract.VerifyRequest) (contract.Profile, error) {
	p.count(&p.verifyHit)

	p.mu.Lock()
	step := next(p.profileSteps, &p.profileN)
	p.mu.Unlock()

	if err := wait(ctx, step.Delay); err != nil {
		return contract.Profile{}, err
	}
	if step.Err != nil {
		return contract.Profile{}, step.Err
	}
	return step.Value, nil
}

// Sign 返回下一次签到结果。
// 同一 AttemptID 串行执行；首次成功后缓存，重复调用不再取脚本。
// 声明 ImageRequired 且请求无图时返回 photo_missing（契约 §4.5）。
func (p *Provider) Sign(ctx context.Context, req contract.SignRequest) (contract.Result, error) {
	p.count(&p.signHit)
	release := p.lockAttempt(req.AttemptID)
	defer release()

	p.mu.Lock()
	if fixed, ok := p.attemptFixed[req.AttemptID]; ok && req.AttemptID != "" {
		p.mu.Unlock()
		if err := wait(ctx, 0); err != nil {
			return contract.Result{}, err
		}
		return fixed, nil
	}
	if cached, ok := p.attempts[req.AttemptID]; ok && req.AttemptID != "" {
		p.mu.Unlock()
		if err := wait(ctx, 0); err != nil {
			return contract.Result{}, err
		}
		return cached, nil
	}
	if p.caps.ImageRequired && req.Image == nil {
		p.mu.Unlock()
		if err := wait(ctx, 0); err != nil {
			return contract.Result{}, err
		}
		return ResultFor(contract.OutcomePhotoMissing), nil
	}
	step := next(p.signSteps, &p.signN)
	p.mu.Unlock()

	if err := wait(ctx, step.Delay); err != nil {
		return contract.Result{}, err
	}
	if step.Err != nil {
		return contract.Result{}, step.Err
	}

	p.mu.Lock()
	if req.AttemptID != "" {
		p.attempts[req.AttemptID] = step.Value
	}
	p.mu.Unlock()
	return step.Value, nil
}

// ExpectedOutcome 声明请求应得的结果，供 conformance 的并发判据比对。
// 顺序：Expect 字段、按 AttemptID 的固定结果、齐次脚本。
// 三者都给不出确定期望时返回 false。
func (p *Provider) ExpectedOutcome(req contract.SignRequest) (contract.Outcome, bool) {
	if p.Expect != nil {
		if outcome, ok := p.Expect(req); ok {
			return outcome, true
		}
	}
	if fixed, ok := p.attemptFixed[req.AttemptID]; ok && req.AttemptID != "" {
		return fixed.Outcome, true
	}
	if len(p.signSteps) == 0 {
		return "", false
	}
	first := p.signSteps[0]
	if first.Err != nil {
		return "", false
	}
	for _, step := range p.signSteps[1:] {
		if step.Err != nil || step.Value.Outcome != first.Value.Outcome {
			return "", false
		}
	}
	return first.Value.Outcome, true
}

// WindowCalls 返回 DiscoverWindow 的调用次数，含被取消的调用。
func (p *Provider) WindowCalls() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.windowHit
}

// TargetCalls 返回 DiscoverTargets 的调用次数，含被取消的调用。
func (p *Provider) TargetCalls() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.targetHit
}

// VerifyCalls 返回 Verify 的调用次数，含被取消的调用。
func (p *Provider) VerifyCalls() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.verifyHit
}

// SignCalls 返回 Sign 的调用次数，含被取消的调用。
func (p *Provider) SignCalls() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.signHit
}

// AllOutcomes 按契约顺序列出全部 14 项中性结果。
// **契约 v1 冻结**；新增结果时必须同步本清单。
func AllOutcomes() []contract.Outcome {
	return []contract.Outcome{
		contract.OutcomeSuccess,
		contract.OutcomeAlreadyDone,
		contract.OutcomePendingUpstream,
		contract.OutcomeNoTarget,
		contract.OutcomeInvalidTarget,
		contract.OutcomeCredentialBad,
		contract.OutcomeDeviceUnbound,
		contract.OutcomeChallenge,
		contract.OutcomeBlocked,
		contract.OutcomeTransient,
		contract.OutcomeRejected,
		contract.OutcomePhotoMissing,
		contract.OutcomePhotoRejected,
		contract.OutcomeUnknown,
	}
}

// OutcomeAdvice 返回契约结果表的默认重试建议。
// 只有 transient 与 challenge 可重试；**unknown 永不自动重试**。
// 数值是示例值，契约只固定分级，不固定秒数。
func OutcomeAdvice(o contract.Outcome) contract.RetryAdvice {
	switch o {
	case contract.OutcomeTransient:
		return contract.RetryAdvice{Retryable: true, After: 30 * time.Second, MaxAttempts: 3}
	case contract.OutcomeChallenge:
		return contract.RetryAdvice{Retryable: true, After: 10 * time.Minute, MaxAttempts: 1}
	default:
		return contract.RetryAdvice{}
	}
}

// ResultFor 按契约生成一项中性结果。
// success 附带证据；其余结果的证据为空。
func ResultFor(o contract.Outcome) contract.Result {
	result := contract.Result{
		Outcome: o,
		Retry:   OutcomeAdvice(o),
		Message: "假 Provider 结果：" + string(o),
	}
	if o == contract.OutcomeSuccess {
		rank := 3
		signedAt := FixedTime
		result.Evidence = contract.Evidence{
			Rank:        &rank,
			SignedAt:    &signedAt,
			Receipt:     "fake-receipt-1",
			PhotoID:     "fake-photo-1",
			PhotoSHA256: "abababababababababababababababababababababababababababababababab",
		}
	}
	return result
}

// ResultSteps 为每个给定结果生成一步预设；无参数时覆盖全部 14 项。
func ResultSteps(outcomes ...contract.Outcome) []Step[contract.Result] {
	if len(outcomes) == 0 {
		outcomes = AllOutcomes()
	}
	steps := make([]Step[contract.Result], 0, len(outcomes))
	for _, o := range outcomes {
		steps = append(steps, Step[contract.Result]{Value: ResultFor(o)})
	}
	return steps
}

// ValidWindow 返回符合契约形状的窗口：30 分钟，来源 platform。
func ValidWindow() contract.Window {
	return contract.Window{
		Start:  FixedTime,
		End:    FixedTime.Add(30 * time.Minute),
		Source: contract.WindowPlatform,
	}
}

// ValidTargets 返回符合契约形状的目标。
func ValidTargets() []contract.Target {
	return []contract.Target{{
		ID:      "target-gate",
		Title:   "校门",
		Address: "示例路 1 号",
		Polygon: []contract.Point{
			{Lng: 121.4700, Lat: 31.2300},
			{Lng: 121.4710, Lat: 31.2300},
			{Lng: 121.4710, Lat: 31.2310},
			{Lng: 121.4700, Lat: 31.2310},
		},
		Center:         contract.Point{Lng: 121.4705, Lat: 31.2305},
		RadiusM:        120,
		RequiresPhoto:  true,
		AttachmentName: "签到照片",
	}}
}

// ValidProfile 返回不含秘密的身份信息。
func ValidProfile() contract.Profile {
	return contract.Profile{
		DisplayName: "测*生",
		SchoolName:  "示例大学",
		StudentID:   "20**0001",
	}
}

// next 取出第 n 项并推进计数；序列为空时返回零值。
func next[T any](steps []Step[T], n *int) Step[T] {
	if len(steps) == 0 {
		return Step[T]{}
	}
	step := steps[*n]
	if *n < len(steps)-1 {
		*n = *n + 1
	}
	return step
}

// wait 先查上下文，再在延迟期间响应取消。
// 零延迟同样走 select：已取消的上下文不能拿到伪结果。
func wait(ctx context.Context, delay time.Duration) error {
	if delay <= 0 {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
			return nil
		}
	}
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

// count 累加一次调用计数。
func (p *Provider) count(n *int) {
	p.mu.Lock()
	*n++
	p.mu.Unlock()
}

// lockAttempt 按 AttemptID 串行化同一尝试的签到。
// 返回解锁函数；AttemptID 为空时返回空函数。
func (p *Provider) lockAttempt(attemptID string) func() {
	if attemptID == "" {
		return func() {}
	}
	p.mu.Lock()
	lock, ok := p.locks[attemptID]
	if !ok {
		lock = &sync.Mutex{}
		p.locks[attemptID] = lock
	}
	p.mu.Unlock()

	lock.Lock()
	return lock.Unlock
}
