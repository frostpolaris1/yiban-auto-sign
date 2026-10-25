package conformance

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/fake"
)

// referenceProvider 返回通过全部判据的参考实现。
// 它用假 Provider 铺满契约词表。
func referenceProvider() contract.Provider {
	return fake.New("conformance-fake", contract.Capabilities{
		WindowDiscovery: true,
		TargetDiscovery: true,
		ImageRequired:   true,
		ManualCaptcha:   true,
		DeviceBinding:   true,
		FixedLocation:   true,
		Rank:            true,
	}).
		OnWindow(fake.Step[contract.Window]{Value: fake.ValidWindow()}).
		OnTargets(fake.Step[[]contract.Target]{Value: fake.ValidTargets()}).
		OnVerify(fake.Step[contract.Profile]{Value: fake.ValidProfile()}).
		OnSign(fake.ResultSteps()...)
}

// findCheck 按名称取判据。
func findCheck(t *testing.T, name string) Check {
	t.Helper()
	for _, check := range Checks() {
		if check.Name == name {
			return check
		}
	}
	t.Fatalf("判据 %q 不存在", name)
	return Check{}
}

// 判据清单固定：删掉一条判据必须让本测试红。
func TestCheckListIsFixed(t *testing.T) {
	want := []string{
		CheckConstructible,
		CheckIDNonEmpty,
		CheckIDStable,
		CheckCapsStable,
		CheckCapsHonored,
		CheckWindowShape,
		CheckWindowCancel,
		CheckTargetsShape,
		CheckTargetsCancel,
		CheckVerifyResult,
		CheckSignResult,
		CheckPhotoMissing,
		CheckPIIAbsent,
		CheckSignRetry,
		CheckSignUnknown,
		CheckSignCancel,
		CheckSignTimeout,
		CheckSignIdempotent,
		CheckSignConcurrent,
	}
	checks := Checks()
	if len(checks) != len(want) {
		t.Fatalf("判据 %d 条, want %d", len(checks), len(want))
	}
	seen := make(map[string]bool, len(checks))
	for i, check := range checks {
		if check.Name != want[i] {
			t.Fatalf("第 %d 条判据 = %q, want %q", i+1, check.Name, want[i])
		}
		if seen[check.Name] {
			t.Fatalf("判据 %q 重复", check.Name)
		}
		seen[check.Name] = true
	}
}

// 参考实现必须全绿。
func TestReferenceProviderPasses(t *testing.T) {
	Run(t, referenceProvider)
}

// 空 ID：id.nonempty 必须红。
type emptyID struct{ contract.Provider }

func (emptyID) ID() string { return "" }

// ID 每次变化：id.stable 必须红。
type unstableID struct {
	contract.Provider
	calls atomic.Int64
}

func (p *unstableID) ID() string {
	if p.calls.Add(1)%2 == 0 {
		return "even"
	}
	return "odd"
}

// 能力每次变化：capabilities.stable 必须红。
type unstableCaps struct {
	contract.Provider
	calls atomic.Int64
}

func (p *unstableCaps) Capabilities() contract.Capabilities {
	return contract.Capabilities{WindowDiscovery: p.calls.Add(1)%2 == 0}
}

// 声明不支持发现，却返回 nil 错误：capabilities.honored 必须红。
type dishonestCaps struct{ contract.Provider }

func (dishonestCaps) Capabilities() contract.Capabilities { return contract.Capabilities{} }

// 窗口结束早于开始：window.shape 必须红。
type invertedWindow struct{ contract.Provider }

func (invertedWindow) DiscoverWindow(context.Context, contract.DiscoverRequest) (contract.Window, error) {
	return contract.Window{
		Start:  fake.FixedTime.Add(time.Hour),
		End:    fake.FixedTime,
		Source: contract.WindowPlatform,
	}, nil
}

// 发现忽略上下文：window.cancel 必须红。
type ignoresCancel struct{ contract.Provider }

func (ignoresCancel) DiscoverWindow(context.Context, contract.DiscoverRequest) (contract.Window, error) {
	return fake.ValidWindow(), nil
}

// 目标纬度越界：targets.shape 必须红。
type badTargets struct{ contract.Provider }

func (badTargets) DiscoverTargets(context.Context, contract.DiscoverRequest) ([]contract.Target, error) {
	return []contract.Target{{
		ID:      "target-bad",
		Center:  contract.Point{Lng: 121.4705, Lat: 99},
		RadiusM: 50,
	}}, nil
}

// 校验失败：verify.result 必须红。
type failingVerify struct{ contract.Provider }

func (failingVerify) Verify(context.Context, contract.VerifyRequest) (contract.Profile, error) {
	return contract.Profile{}, errors.New("校验失败")
}

// 结果不在词表：sign.result 必须红。
type badOutcome struct{ contract.Provider }

func (badOutcome) Sign(context.Context, contract.SignRequest) (contract.Result, error) {
	return contract.Result{Outcome: contract.Outcome("done")}, nil
}

// 证据带明文手机号：pii.absent 必须红。
type leaksPhone struct{ contract.Provider }

func (leaksPhone) Sign(context.Context, contract.SignRequest) (contract.Result, error) {
	return contract.Result{
		Outcome:  contract.OutcomeSuccess,
		Evidence: contract.Evidence{Raw: map[string]any{"note": testCredential().Phone}},
	}, nil
}

// unknown 可重试：sign.unknown 必须红。
type retriesUnknown struct{ contract.Provider }

func (retriesUnknown) Sign(context.Context, contract.SignRequest) (contract.Result, error) {
	return contract.Result{
		Outcome: contract.OutcomeUnknown,
		Retry:   contract.RetryAdvice{Retryable: true, MaxAttempts: 3},
	}, nil
}

// 取消后仍报告成功：sign.cancel 必须红。
type ignoresCancelOnSign struct{ contract.Provider }

func (ignoresCancelOnSign) Sign(context.Context, contract.SignRequest) (contract.Result, error) {
	return contract.Result{Outcome: contract.OutcomeSuccess}, nil
}

// 目标发现忽略上下文：targets.cancel 必须红。
type ignoresCancelOnTargets struct{ contract.Provider }

func (ignoresCancelOnTargets) DiscoverTargets(context.Context, contract.DiscoverRequest) ([]contract.Target, error) {
	return fake.ValidTargets(), nil
}

// transient 却标记不可重试：sign.retry 必须红。
type nonRetryableTransient struct{ contract.Provider }

func (nonRetryableTransient) Sign(context.Context, contract.SignRequest) (contract.Result, error) {
	return contract.Result{Outcome: contract.OutcomeTransient}, nil
}

// 同一 AttemptID 两次结果不同：sign.idempotent 必须红。
type unstableSign struct {
	contract.Provider
	calls atomic.Int64
}

func (p *unstableSign) Sign(context.Context, contract.SignRequest) (contract.Result, error) {
	if p.calls.Add(1)%2 == 0 {
		return contract.Result{Outcome: contract.OutcomeAlreadyDone}, nil
	}
	return contract.Result{Outcome: contract.OutcomeSuccess}, nil
}

// 声明期望 success，实际返回 already_done：sign.concurrent 必须红。
type crossTalk struct{ contract.Provider }

func (crossTalk) ExpectedOutcome(contract.SignRequest) (contract.Outcome, bool) {
	return contract.OutcomeSuccess, true
}

func (crossTalk) Sign(context.Context, contract.SignRequest) (contract.Result, error) {
	return contract.Result{Outcome: contract.OutcomeAlreadyDone}, nil
}

// 证据带分隔手机号：pii.absent 必须红。
// 该形状与 W2C-R4-01 修复的形状相同；判据必须与被保护的边界同口径。
type leaksSeparatedPhone struct{ contract.Provider }

func (leaksSeparatedPhone) Sign(context.Context, contract.SignRequest) (contract.Result, error) {
	return contract.Result{
		Outcome:  contract.OutcomeSuccess,
		Evidence: contract.Evidence{Raw: map[string]any{"note": "139-0013-9000"}},
	}, nil
}

// 同一 AttemptID 的 Outcome 相同，但证据字段变化：sign.idempotent 必须红。
// 只比较 Outcome 的判据会放行这一形状。
type unstableEvidence struct {
	contract.Provider
	calls atomic.Int64
}

func (p *unstableEvidence) Sign(context.Context, contract.SignRequest) (contract.Result, error) {
	receipt := "receipt-1"
	if p.calls.Add(1)%2 == 0 {
		receipt = "receipt-2"
	}
	return contract.Result{
		Outcome:  contract.OutcomeSuccess,
		Evidence: contract.Evidence{Receipt: receipt},
	}, nil
}

// 声明需要图片，无图请求却报成功：sign.photo_missing 必须红。
type signsWithoutPhoto struct{ contract.Provider }

func (signsWithoutPhoto) Sign(context.Context, contract.SignRequest) (contract.Result, error) {
	return contract.Result{Outcome: contract.OutcomeSuccess}, nil
}

// 忽略上下文与期限：sign.timeout 必须红。
type ignoresDeadline struct{ contract.Provider }

func (ignoresDeadline) Sign(context.Context, contract.SignRequest) (contract.Result, error) {
	return contract.Result{Outcome: contract.OutcomeSuccess}, nil
}

// 入口查上下文，调用中忽略：延迟返回成功。sign.timeout 必须红。
type lateSuccess struct{ contract.Provider }

func (lateSuccess) Sign(ctx context.Context, _ contract.SignRequest) (contract.Result, error) {
	if err := ctx.Err(); err != nil {
		return contract.Result{}, err
	}
	time.Sleep(60 * time.Millisecond)
	return contract.Result{Outcome: contract.OutcomeSuccess}, nil
}

// 不实现 Expecter：sign.concurrent 必须拒绝认证（G2）。
type withoutExpecter struct{ contract.Provider }

// 判据内 panic：runCheck 必须转成同格式失败。
type panicsOnID struct{ contract.Provider }

func (panicsOnID) ID() string { panic("boom") }

// 坏提供方必须被对应判据抓住，失败文本统一，且原因可核对。
// reason 是该判据特有的原因子串；只比前缀会让「因别的原因红」也通过。
func TestBrokenProvidersAreCaught(t *testing.T) {
	cases := []struct {
		name        string
		want        string
		reason      string
		newProvider NewProvider
	}{
		{"构造器返回 nil", CheckConstructible, "构造器返回 nil",
			func() contract.Provider { return nil }},
		{"空 ID", CheckIDNonEmpty, "ID() 为空",
			func() contract.Provider { return emptyID{referenceProvider()} }},
		{"ID 不稳定", CheckIDStable, "首值",
			func() contract.Provider { return &unstableID{Provider: referenceProvider()} }},
		{"能力不稳定", CheckCapsStable, "两次能力不同",
			func() contract.Provider { return &unstableCaps{Provider: referenceProvider()} }},
		{"能力声明不实", CheckCapsHonored, "未声明 WindowDiscovery",
			func() contract.Provider { return dishonestCaps{referenceProvider()} }},
		{"窗口起止颠倒", CheckWindowShape, "不晚于开始",
			func() contract.Provider { return invertedWindow{referenceProvider()} }},
		{"发现忽略取消", CheckWindowCancel, "want context.Canceled",
			func() contract.Provider { return ignoresCancel{referenceProvider()} }},
		{"目标发现忽略取消", CheckTargetsCancel, "want context.Canceled",
			func() contract.Provider { return ignoresCancelOnTargets{referenceProvider()} }},
		{"目标坐标越界", CheckTargetsShape, "坐标无效",
			func() contract.Provider { return badTargets{referenceProvider()} }},
		{"校验失败", CheckVerifyResult, "Verify 返回错误",
			func() contract.Provider { return failingVerify{referenceProvider()} }},
		{"结果词表越界", CheckSignResult, "不在契约词表内",
			func() contract.Provider { return badOutcome{referenceProvider()} }},
		{"证据泄漏手机号", CheckPIIAbsent, "泄漏手机号",
			func() contract.Provider { return leaksPhone{referenceProvider()} }},
		{"证据泄漏分隔手机号", CheckPIIAbsent, "明文手机号",
			func() contract.Provider { return leaksSeparatedPhone{referenceProvider()} }},
		{"unknown 可重试", CheckSignUnknown, "unknown 被标记可重试",
			func() contract.Provider { return retriesUnknown{referenceProvider()} }},
		{"transient 不可重试", CheckSignRetry, "transient 必须可重试",
			func() contract.Provider { return nonRetryableTransient{referenceProvider()} }},
		{"取消后仍成功", CheckSignCancel, "want context.Canceled",
			func() contract.Provider { return ignoresCancelOnSign{referenceProvider()} }},
		{"重复调用变形", CheckSignIdempotent, "同一 AttemptID 两次结果不同",
			func() contract.Provider { return &unstableSign{Provider: referenceProvider()} }},
		{"重复调用证据变化", CheckSignIdempotent, "同一 AttemptID 两次结果不同",
			func() contract.Provider { return &unstableEvidence{Provider: referenceProvider()} }},
		{"并发结果串扰", CheckSignConcurrent, "并发结果串扰",
			func() contract.Provider { return crossTalk{referenceProvider()} }},
		{"未实现 Expecter", CheckSignConcurrent, "Expecter",
			func() contract.Provider { return withoutExpecter{referenceProvider()} }},
		{"无图仍报成功", CheckPhotoMissing, "want photo_missing",
			func() contract.Provider { return signsWithoutPhoto{referenceProvider()} }},
		{"忽略期限", CheckSignTimeout, "期限已过仍返回 nil 错误",
			func() contract.Provider { return ignoresDeadline{referenceProvider()} }},
		{"在途取消后返回成功", CheckSignTimeout, "取消后仍返回 nil 错误",
			func() contract.Provider { return lateSuccess{referenceProvider()} }},
		{"判据内 panic", CheckIDNonEmpty, "panic: boom",
			func() contract.Provider { return panicsOnID{referenceProvider()} }},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := runCheck(findCheck(t, tc.want), tc.newProvider)
			if err == nil {
				t.Fatalf("判据 %s 未报红", tc.want)
			}
			prefix := "conformance: " + tc.want + ": "
			if !strings.HasPrefix(err.Error(), prefix) {
				t.Fatalf("失败文本 %q 未以 %q 开头", err.Error(), prefix)
			}
			if !strings.Contains(err.Error(), tc.reason) {
				t.Fatalf("失败文本 %q 未含原因 %q", err.Error(), tc.reason)
			}
		})
	}
}

// 并发判据的强路径：齐次脚本给出确定期望，声明与兑现不一致必须红。
func TestConcurrencyExpectationIsEnforced(t *testing.T) {
	homogeneous := func() contract.Provider {
		return fake.New("conformance-homogeneous", contract.Capabilities{}).
			OnSign(fake.ResultSteps(contract.OutcomeSuccess, contract.OutcomeSuccess)...)
	}

	check := findCheck(t, CheckSignConcurrent)
	if err := runCheck(check, homogeneous); err != nil {
		t.Fatalf("齐次脚本应通过并发判据: %v", err)
	}

	expecter, ok := homogeneous().(Expecter)
	if !ok {
		t.Fatal("齐次脚本的假 Provider 必须实现 Expecter")
	}
	if _, declared := expecter.ExpectedOutcome(signRequest("attempt-concurrent-0")); !declared {
		t.Fatal("齐次脚本未给出期望，并发判据走了弱路径")
	}

	if err := runCheck(check, func() contract.Provider { return crossTalk{referenceProvider()} }); err == nil {
		t.Fatal("声明期望却不兑现的提供方必须报红")
	}
}

// 检测口径的正例：证据里出现毫秒时间戳不得判为泄漏。
type echoesTimestamp struct{ contract.Provider }

func (echoesTimestamp) Sign(context.Context, contract.SignRequest) (contract.Result, error) {
	return contract.Result{
		Outcome:  contract.OutcomeSuccess,
		Evidence: contract.Evidence{Raw: map[string]any{"note": "1735689600000"}},
	}, nil
}

// 取消后返回非 success 的伪结果：sign.cancel 必须红（与 sign.timeout 同口径）。
type pseudoOutcomeOnCancel struct{ contract.Provider }

func (pseudoOutcomeOnCancel) Sign(ctx context.Context, _ contract.SignRequest) (contract.Result, error) {
	if err := ctx.Err(); err != nil {
		return contract.Result{Outcome: contract.OutcomeAlreadyDone}, err
	}
	return contract.Result{Outcome: contract.OutcomeSuccess}, nil
}

// 泄漏邮箱：pii.absent 必须红，原因是明文邮箱。
type leaksEmail struct{ contract.Provider }

func (leaksEmail) Sign(context.Context, contract.SignRequest) (contract.Result, error) {
	return contract.Result{
		Outcome:  contract.OutcomeSuccess,
		Evidence: contract.Evidence{Raw: map[string]any{"note": "a@b.com"}},
	}, nil
}

func TestDetectionCaliberAndCapabilitySkips(t *testing.T) {
	// 时间戳放行。
	if err := runCheck(findCheck(t, CheckPIIAbsent), func() contract.Provider {
		return echoesTimestamp{referenceProvider()}
	}); err != nil {
		t.Fatalf("毫秒时间戳被判为泄漏: %v", err)
	}

	// 邮箱泄漏必须红，且原因指向邮箱。
	err := runCheck(findCheck(t, CheckPIIAbsent), func() contract.Provider {
		return leaksEmail{referenceProvider()}
	})
	if err == nil || !strings.Contains(err.Error(), "明文邮箱") {
		t.Fatalf("邮箱泄漏未按预期红: %v", err)
	}

	// 取消后的伪结果必须红（旧口径只禁 success，会放过 already_done）。
	err = runCheck(findCheck(t, CheckSignCancel), func() contract.Provider {
		return pseudoOutcomeOnCancel{referenceProvider()}
	})
	if err == nil || !strings.Contains(err.Error(), "伪结果") {
		t.Fatalf("取消后的伪结果未红: %v", err)
	}

	// 未声明能力的四条判据返回 errNotApplicable，不静默通过。
	capsLess := func() contract.Provider {
		return fake.New("caps-less", contract.Capabilities{}).
			OnSign(fake.ResultSteps()...)
	}
	for _, name := range []string{CheckWindowShape, CheckWindowCancel, CheckTargetsShape, CheckTargetsCancel, CheckPhotoMissing} {
		err := runCheck(findCheck(t, name), capsLess)
		if !errors.Is(err, errNotApplicable) {
			t.Fatalf("%s 未返回 errNotApplicable: %v", name, err)
		}
	}
}

// Run 对未声明能力的提供方必须打印跳过行（-v 下可见），且不算失败。
func TestRunPrintsSkips(t *testing.T) {
	Run(t, func() contract.Provider {
		return fake.New("caps-less-run", contract.Capabilities{}).
			OnSign(fake.ResultSteps()...)
	})
}

// 看门狗入库钉（R4 3.4）：阻塞的提供方必须在上限附近得到失败，不得挂起。
// 超时变量注入短值，不靠真等 15 秒。
func TestCriterionWatchdogFailsBlockingProvider(t *testing.T) {
	previous := criterionTimeout
	criterionTimeout = 150 * time.Millisecond
	t.Cleanup(func() { criterionTimeout = previous })

	blocking := func() contract.Provider { return blocksSign{referenceProvider()} }
	err := runCheck(findCheck(t, CheckSignResult), blocking)
	if !errors.Is(err, errCriterionTimeout) {
		t.Fatalf("阻塞提供方未触发判据超时: %v", err)
	}
}

// 看门狗有界：阻塞判据在注入上限附近返回超时哨兵，不悬挂。
func TestCriterionWatchdogBoundsBlockingProvider(t *testing.T) {
	previous := criterionTimeout
	criterionTimeout = 120 * time.Millisecond
	t.Cleanup(func() { criterionTimeout = previous })

	clk := coreclock.System{}
	started := clk.Now()
	err := runCheck(findCheck(t, CheckSignResult), func() contract.Provider {
		return blocksSign{referenceProvider()}
	})
	elapsed := clk.Now().Sub(started)
	if !errors.Is(err, errCriterionTimeout) {
		t.Fatalf("阻塞提供方未触发判据超时: %v", err)
	}
	if elapsed < criterionTimeout || elapsed > criterionTimeout+time.Second {
		t.Fatalf("判据耗时 %s；应在 %s 附近", elapsed, criterionTimeout)
	}
}

// 停跑决策：超时哨兵要求停跑，其余错误不停。
func TestStopAfterDecision(t *testing.T) {
	if !stopAfter(fmt.Errorf("%s: %w", "conformance: x: 未完成", errCriterionTimeout)) {
		t.Fatal("超时哨兵未触发停跑")
	}
	if stopAfter(errors.New("普通失败")) || stopAfter(nil) {
		t.Fatal("普通错误不应停跑")
	}
}

// blocksSign 在 Sign 上永久阻塞，其余方法正常。
type blocksSign struct{ contract.Provider }

func (blocksSign) Sign(context.Context, contract.SignRequest) (contract.Result, error) {
	select {}
}

// 阻塞的未声明能力：判据必须在调用之前判能力（G1），
// 否则会先调阻塞方法、耗满看门狗再报超时。
type blocksDiscovery struct{ contract.Provider }

func (blocksDiscovery) DiscoverWindow(context.Context, contract.DiscoverRequest) (contract.Window, error) {
	select {}
}

func TestUndeclaredCapabilityIsNotCalled(t *testing.T) {
	previous := criterionTimeout
	criterionTimeout = 150 * time.Millisecond
	t.Cleanup(func() { criterionTimeout = previous })

	provider := func() contract.Provider {
		return blocksDiscovery{fake.New("blocks-discovery", contract.Capabilities{})}
	}
	clk := coreclock.System{}
	started := clk.Now()
	err := runCheck(findCheck(t, CheckWindowCancel), provider)
	elapsed := clk.Now().Sub(started)
	if !errors.Is(err, errNotApplicable) {
		t.Fatalf("未声明能力仍被调用: %v", err)
	}
	if elapsed > criterionTimeout {
		t.Fatalf("判据耗时 %s；能力检查应在调用之前", elapsed)
	}
}
