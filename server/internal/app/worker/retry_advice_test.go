package worker_test

// 提供方重试建议（contract.Result.Retry）的接线。
//
// # 判据
// 建议只参与可重试面的两条判据：
//   - MaxAttempts 更小者为准（提供方说"重试一次"就只重试一次）；
//   - After 是等待下限（提供方说"至少 5 分钟"就不许 60 秒后再打）；
// 错误路径拿不到 Result，必须零建议 ⇒ 完全保持宿主缺省行为。

import (
	"context"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// 建议的冷却下限抬掉落点：默认 base 60s，建议 4min。
func TestRetryAdviceRaisesBackoffFloor(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	signer.outcome = contract.OutcomeTransient
	signer.retry = contract.RetryAdvice{Retryable: true, MaxAttempts: 3, After: 4 * time.Minute}
	svc := newService(t, st, signer, fakeLimiter{allow: true})

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	runAt, ok := st.requeued["t1"]
	if !ok {
		t.Fatal("应退回 pending 等重试")
	}
	if _, done := st.finished["t1"]; done {
		t.Fatal("预算未耗尽不该落终态")
	}
	low, high := testNow().Add(4*time.Minute), testNow().Add(6*time.Minute)
	if runAt.Before(low) || runAt.After(high) {
		t.Fatalf("落点 = %v, want [%v,%v]（60s 基础间隔被 4min 下限抬起）", runAt, low, high)
	}
}

// 建议次数比宿主更小：第一次失败即落 failed，不再重排。
func TestRetryAdviceCapsAttempts(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	signer.outcome = contract.OutcomeTransient
	signer.retry = contract.RetryAdvice{Retryable: true, MaxAttempts: 1}
	svc := newService(t, st, signer, fakeLimiter{allow: true})

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if got := st.finished["t1"]; got == "" {
		t.Fatal("建议 MaxAttempts=1（宿主预算 3）应直接落终态")
	}
	if _, requeued := st.requeued["t1"]; requeued {
		t.Fatal("预算耗尽后不得再重排")
	}
}

// 建议次数比宿主更大：宿主上限优先，仍按 3 次预算重排。
func TestRetryAdviceDoesNotRaiseAttempts(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	signer.outcome = contract.OutcomeTransient
	signer.retry = contract.RetryAdvice{Retryable: true, MaxAttempts: 9}
	svc := newService(t, st, signer, fakeLimiter{allow: true})

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	runAt, ok := st.requeued["t1"]
	if !ok {
		t.Fatal("宿主预算内应重排")
	}
	low, high := testNow().Add(30*time.Second), testNow().Add(90*time.Second)
	if runAt.Before(low) || runAt.After(high) {
		t.Fatalf("落点 = %v, want [%v,%v]（宿主 60s 基础间隔）", runAt, low, high)
	}
}

// 零值建议（老提供方 / 错误路径）：宿主缺省行为，不加下限也不改次数。
func TestRetryAdviceZeroKeepsHostPolicy(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	signer.outcome = contract.OutcomeTransient
	svc := newService(t, st, signer, fakeLimiter{allow: true})

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	runAt, ok := st.requeued["t1"]
	if !ok {
		t.Fatal("零值建议不该阻止重排")
	}
	low, high := testNow().Add(30*time.Second), testNow().Add(90*time.Second)
	if runAt.Before(low) || runAt.After(high) {
		t.Fatalf("落点 = %v, want [%v,%v]", runAt, low, high)
	}
}

// 建议下限超出窗口尾段份额：当日放弃，不排到窗口外。
func TestRetryAdviceFloorBeyondTailGivesUp(t *testing.T) {
	st := newStore(baseTask())
	win := openWindow()
	win.End = testNow().Add(10 * time.Minute) // cap = 3min < 建议 5min
	st.windows["st-1|2026-10-29"] = win
	signer := defaultSigner()
	signer.outcome = contract.OutcomeTransient
	signer.retry = contract.RetryAdvice{Retryable: true, MaxAttempts: 3, After: 5 * time.Minute}
	svc := newService(t, st, signer, fakeLimiter{allow: true})

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if _, requeued := st.requeued["t1"]; requeued {
		t.Fatal("尾段放不下下限，应放弃而不是越窗重排")
	}
	if _, done := st.finished["t1"]; !done {
		t.Fatal("放弃应落终态")
	}
}
