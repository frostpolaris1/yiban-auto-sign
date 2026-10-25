package fake

import (
	"context"
	"errors"
	"fmt"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// discoverRequest 返回判据用的固定发现请求。
func discoverRequest() contract.DiscoverRequest {
	return contract.DiscoverRequest{
		Account:    contract.AccountRef{ID: "acct-1", SchoolID: "school-1", Platform: "fake"},
		Credential: contract.Credential{Phone: "13800138000", Password: "secret-1"},
	}
}

// signRequest 返回判据用的固定签到请求。
func signRequest(attemptID string) contract.SignRequest {
	return contract.SignRequest{
		AttemptID:  attemptID,
		Account:    discoverRequest().Account,
		Credential: discoverRequest().Credential,
		Window:     ValidWindow(),
		Target:     ValidTargets()[0],
		Location:   contract.Location{Mode: contract.LocationFixed, Lng: 121.4705, Lat: 31.2305},
	}
}

// 序列语义：第 N 次取第 N 项，用尽后重复最后一项。
func TestSignSequenceRepeatsLast(t *testing.T) {
	p := New("fake", contract.Capabilities{}).
		OnSign(ResultSteps(contract.OutcomeSuccess, contract.OutcomeAlreadyDone)...)

	wants := []contract.Outcome{
		contract.OutcomeSuccess,
		contract.OutcomeAlreadyDone,
		contract.OutcomeAlreadyDone,
	}
	for i, want := range wants {
		got, err := p.Sign(context.Background(), signRequest(fmt.Sprintf("attempt-%d", i)))
		if err != nil {
			t.Fatalf("第 %d 次 Sign 返回错误: %v", i+1, err)
		}
		if got.Outcome != want {
			t.Fatalf("第 %d 次结果 = %q, want %q", i+1, got.Outcome, want)
		}
	}
	if got := p.SignCalls(); got != 3 {
		t.Fatalf("SignCalls = %d, want 3", got)
	}
}

// 同一 AttemptID 重复调用返回首次结果，且不推进脚本。
func TestSignIsIdempotentPerAttempt(t *testing.T) {
	p := New("fake", contract.Capabilities{}).
		OnSign(ResultSteps(contract.OutcomeSuccess, contract.OutcomeTransient)...)

	first, err := p.Sign(context.Background(), signRequest("attempt-a"))
	if err != nil {
		t.Fatalf("首次 Sign 返回错误: %v", err)
	}
	second, err := p.Sign(context.Background(), signRequest("attempt-a"))
	if err != nil {
		t.Fatalf("重复 Sign 返回错误: %v", err)
	}
	if first.Outcome != contract.OutcomeSuccess || second.Outcome != contract.OutcomeSuccess {
		t.Fatalf("重复调用结果 = %q / %q, want 两次 %q",
			first.Outcome, second.Outcome, contract.OutcomeSuccess)
	}
	// 第二次调用若推进了脚本，后续 attempt-b 会拿到序列最后一项。
	other, err := p.Sign(context.Background(), signRequest("attempt-b"))
	if err != nil {
		t.Fatalf("attempt-b Sign 返回错误: %v", err)
	}
	if other.Outcome != contract.OutcomeTransient {
		t.Fatalf("attempt-b 结果 = %q, want %q", other.Outcome, contract.OutcomeTransient)
	}
}

// 错误不进入幂等缓存：同一 AttemptID 可以在重试后成功。
func TestSignErrorsAreNotCached(t *testing.T) {
	wantErr := errors.New("上游抖动")
	p := New("fake", contract.Capabilities{}).
		OnSign(
			Step[contract.Result]{Err: wantErr},
			ResultSteps(contract.OutcomeSuccess)[0],
		)

	if _, err := p.Sign(context.Background(), signRequest("attempt-a")); !errors.Is(err, wantErr) {
		t.Fatalf("首次错误 = %v, want %v", err, wantErr)
	}
	got, err := p.Sign(context.Background(), signRequest("attempt-a"))
	if err != nil {
		t.Fatalf("重试 Sign 返回错误: %v", err)
	}
	if got.Outcome != contract.OutcomeSuccess {
		t.Fatalf("重试结果 = %q, want %q", got.Outcome, contract.OutcomeSuccess)
	}
}

// 按 AttemptID 的覆盖优先于序列，并且重复调用稳定。
func TestSignAttemptOverride(t *testing.T) {
	p := New("fake", contract.Capabilities{}).
		OnSign(ResultSteps(contract.OutcomeSuccess)...).
		OnAttempt("attempt-fixed", ResultFor(contract.OutcomeCredentialBad))

	for i := 0; i < 2; i++ {
		got, err := p.Sign(context.Background(), signRequest("attempt-fixed"))
		if err != nil {
			t.Fatalf("第 %d 次 Sign 返回错误: %v", i+1, err)
		}
		if got.Outcome != contract.OutcomeCredentialBad {
			t.Fatalf("第 %d 次结果 = %q, want %q", i+1, got.Outcome, contract.OutcomeCredentialBad)
		}
	}
	other, err := p.Sign(context.Background(), signRequest("attempt-other"))
	if err != nil {
		t.Fatalf("attempt-other Sign 返回错误: %v", err)
	}
	if other.Outcome != contract.OutcomeSuccess {
		t.Fatalf("attempt-other 结果 = %q, want %q", other.Outcome, contract.OutcomeSuccess)
	}
}

// 延迟期间响应取消，不得等满 10 秒。
func TestSignDelayHonorsContext(t *testing.T) {
	p := New("fake", contract.Capabilities{}).
		OnSign(Step[contract.Result]{Value: ResultFor(contract.OutcomeSuccess), Delay: 10 * time.Second})

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	start := time.Now()
	if _, err := p.Sign(ctx, signRequest("attempt-a")); !errors.Is(err, context.Canceled) {
		t.Fatalf("错误 = %v, want context.Canceled", err)
	}
	if elapsed := time.Since(start); elapsed > time.Second {
		t.Fatalf("取消耗时 %s, want ≤1s", elapsed)
	}
}

// 延迟必须真实生效；零延迟的替身测不到等待行为。
func TestSignDelayElapses(t *testing.T) {
	const delay = 40 * time.Millisecond
	p := New("fake", contract.Capabilities{}).
		OnSign(Step[contract.Result]{Value: ResultFor(contract.OutcomeSuccess), Delay: delay})

	start := time.Now()
	if _, err := p.Sign(context.Background(), signRequest("attempt-a")); err != nil {
		t.Fatalf("Sign 返回错误: %v", err)
	}
	if elapsed := time.Since(start); elapsed < delay/2 {
		t.Fatalf("耗时 %s, want ≥%s", elapsed, delay/2)
	}
}

// 空 AttemptID 关闭幂等缓存，每次调用都取一项脚本。
func TestEmptyAttemptIDIsNotCached(t *testing.T) {
	p := New("fake", contract.Capabilities{}).
		OnSign(ResultSteps(contract.OutcomeSuccess, contract.OutcomeAlreadyDone)...)

	for i, want := range []contract.Outcome{contract.OutcomeSuccess, contract.OutcomeAlreadyDone} {
		got, err := p.Sign(context.Background(), signRequest(""))
		if err != nil {
			t.Fatalf("第 %d 次 Sign 返回错误: %v", i+1, err)
		}
		if got.Outcome != want {
			t.Fatalf("第 %d 次结果 = %q, want %q", i+1, got.Outcome, want)
		}
	}
}

// 未配置脚本的方法返回零值与 nil 错误。
func TestUnscriptedReturnsZero(t *testing.T) {
	p := New("fake", contract.Capabilities{})
	if w, err := p.DiscoverWindow(context.Background(), discoverRequest()); err != nil || !w.Start.IsZero() {
		t.Fatalf("未配置窗口 = %+v, %v; want 零值, nil", w, err)
	}
	if got, err := p.DiscoverTargets(context.Background(), discoverRequest()); err != nil || got != nil {
		t.Fatalf("未配置目标 = %v, %v; want nil, nil", got, err)
	}
}

// 发现与校验同样响应取消。
func TestDiscoverAndVerifyHonorContext(t *testing.T) {
	p := New("fake", contract.Capabilities{}).
		OnWindow(Step[contract.Window]{Value: ValidWindow()}).
		OnTargets(Step[[]contract.Target]{Value: ValidTargets()}).
		OnVerify(Step[contract.Profile]{Value: ValidProfile()})

	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	if _, err := p.DiscoverWindow(ctx, discoverRequest()); !errors.Is(err, context.Canceled) {
		t.Fatalf("DiscoverWindow 错误 = %v, want context.Canceled", err)
	}
	if _, err := p.DiscoverTargets(ctx, discoverRequest()); !errors.Is(err, context.Canceled) {
		t.Fatalf("DiscoverTargets 错误 = %v, want context.Canceled", err)
	}
	if _, err := p.Verify(ctx, contract.VerifyRequest{Account: discoverRequest().Account, Credential: discoverRequest().Credential}); !errors.Is(err, context.Canceled) {
		t.Fatalf("Verify 错误 = %v, want context.Canceled", err)
	}
}

// 结果词表必须覆盖契约全部 13 项，且 Advice 与契约表一致。
func TestOutcomeTablesFollowContract(t *testing.T) {
	all := AllOutcomes()
	if len(all) != 13 {
		t.Fatalf("AllOutcomes 有 %d 项, want 13", len(all))
	}
	seen := make(map[contract.Outcome]bool, len(all))
	for _, o := range all {
		if !o.Valid() {
			t.Fatalf("%q 不在契约词表内", o)
		}
		if seen[o] {
			t.Fatalf("%q 重复", o)
		}
		seen[o] = true

		advice := OutcomeAdvice(o)
		switch o {
		case contract.OutcomeTransient, contract.OutcomeChallenge:
			if !advice.Retryable {
				t.Fatalf("%q 应可重试", o)
			}
			if advice.MaxAttempts < 1 {
				t.Fatalf("%q 可重试但 MaxAttempts = 0", o)
			}
		default:
			if advice.Retryable {
				t.Fatalf("%q 不应自动重试", o)
			}
		}
	}
}

// ResultSteps 与 AllOutcomes 一一对应。
func TestResultStepsCoverAllOutcomes(t *testing.T) {
	steps := ResultSteps()
	all := AllOutcomes()
	if len(steps) != len(all) {
		t.Fatalf("ResultSteps 有 %d 项, want %d", len(steps), len(all))
	}
	for i, step := range steps {
		if step.Err != nil {
			t.Fatalf("第 %d 步含错误: %v", i+1, step.Err)
		}
		if step.Value.Outcome != all[i] {
			t.Fatalf("第 %d 步结果 = %q, want %q", i+1, step.Value.Outcome, all[i])
		}
	}
}

// 并发判据的期望：脚本每一项都相同时给出期望结果，否则不给。
func TestExpectedOutcome(t *testing.T) {
	homogeneous := New("fake", contract.Capabilities{}).
		OnSign(ResultSteps(contract.OutcomeSuccess, contract.OutcomeSuccess)...)
	if got, ok := homogeneous.ExpectedOutcome(signRequest("attempt-x")); !ok || got != contract.OutcomeSuccess {
		t.Fatalf("齐次脚本期望 = %q, %v; want %q, true", got, ok, contract.OutcomeSuccess)
	}

	mixed := New("fake", contract.Capabilities{}).OnSign(ResultSteps()...)
	if got, ok := mixed.ExpectedOutcome(signRequest("attempt-x")); ok {
		t.Fatalf("混合脚本期望 = %q, true; want false", got)
	}

	overridden := New("fake", contract.Capabilities{}).
		OnSign(ResultSteps()...).
		OnAttempt("attempt-y", ResultFor(contract.OutcomeAlreadyDone))
	if got, ok := overridden.ExpectedOutcome(signRequest("attempt-y")); !ok || got != contract.OutcomeAlreadyDone {
		t.Fatalf("覆盖期望 = %q, %v; want %q, true", got, ok, contract.OutcomeAlreadyDone)
	}

	custom := New("fake", contract.Capabilities{})
	custom.Expect = func(contract.SignRequest) (contract.Outcome, bool) {
		return contract.OutcomeChallenge, true
	}
	if got, ok := custom.ExpectedOutcome(signRequest("attempt-z")); !ok || got != contract.OutcomeChallenge {
		t.Fatalf("Expect 期望 = %q, %v; want %q, true", got, ok, contract.OutcomeChallenge)
	}
}

// 参考夹具必须符合契约形状。
func TestReferenceFixtures(t *testing.T) {
	w := ValidWindow()
	if w.Start.IsZero() || !w.End.After(w.Start) || w.Source != contract.WindowPlatform {
		t.Fatalf("窗口夹具形状错误: %+v", w)
	}
	targets := ValidTargets()
	if len(targets) == 0 {
		t.Fatal("目标夹具为空")
	}
	for _, target := range targets {
		if target.ID == "" {
			t.Fatalf("目标夹具缺少 ID: %+v", target)
		}
	}
	profile := ValidProfile()
	if profile.DisplayName == "" {
		t.Fatalf("身份夹具缺少展示名: %+v", profile)
	}
}
