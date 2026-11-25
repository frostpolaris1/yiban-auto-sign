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
	"fmt"
	"testing"
	"time"

	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
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

// 只给等待下限、不给次数（MaxAttempts=0）：宿主预算不变，只抬起等待下限。
// 老提供方/局部建议的常见形态，不能被当成"不重试"。
func TestRetryAdviceFloorWithoutLimitKeepsHostBudget(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	signer.outcome = contract.OutcomeTransient
	signer.retry = contract.RetryAdvice{After: 4 * time.Minute}
	svc := newService(t, st, signer, fakeLimiter{allow: true})

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	runAt, ok := st.requeued["t1"]
	if !ok {
		t.Fatal("MaxAttempts=0 是「没有建议」，宿主预算内必须重排")
	}
	if _, done := st.finished["t1"]; done {
		t.Fatal("MaxAttempts=0 不该压缩宿主次数")
	}
	low, high := testNow().Add(4*time.Minute), testNow().Add(6*time.Minute)
	if runAt.Before(low) || runAt.After(high) {
		t.Fatalf("落点 = %v, want [%v,%v]（After 作为下限，不改变次数）", runAt, low, high)
	}
}

// 整段尝试耗过窗口终点：结算时刻已关窗，必须落 failed，
// 不能把 run_at 排到窗口外再撞一次平台。
func TestBackoffAfterWindowClosedGivesUp(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	signer.outcome = contract.OutcomeTransient
	clk := &stepClock{t: testNow()}
	signer.onSign = func() { clk.t = clk.t.Add(60 * time.Minute) }
	svc := newServiceWithClock(t, st, signer, fakeLimiter{allow: true}, clk)

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if _, requeued := st.requeued["t1"]; requeued {
		t.Fatal("结算时窗口已关，不得重排")
	}
	if _, done := st.finished["t1"]; !done {
		t.Fatal("结算时窗口已关，应落终态")
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

// 限次建议不再依赖 Retryable：conformance 不允许 pending_upstream/unknown
// 标 Retryable=true（其余结果标 true 直接判红），但次数照样能收口——
// 这是 issue #413 的判定点：不重试写 MaxAttempts=1，与 Retryable 无关。
func TestRetryAdviceLimitsWithoutRetryable(t *testing.T) {
	for _, outcome := range []contract.Outcome{contract.OutcomePendingUpstream, contract.OutcomeUnknown} {
		for _, limit := range []int{1, 2} {
			t.Run(fmt.Sprintf("%s/max=%d", outcome, limit), func(t *testing.T) {
				st := newStore(baseTask())
				st.windows["st-1|2026-10-29"] = openWindow()
				signer := defaultSigner()
				signer.outcome = outcome
				// 刻意 Retryable=false：回归点就在这里。
				signer.retry = contract.RetryAdvice{Retryable: false, MaxAttempts: limit}
				clk := &stepClock{t: testNow()}
				svc := newServiceWithClock(t, st, signer, fakeLimiter{allow: true}, clk)

				if _, err := svc.Step(context.Background(), 10); err != nil {
					t.Fatalf("首轮 Step: %v", err)
				}
				if limit == 1 {
					if _, requeued := st.requeued["t1"]; requeued {
						t.Fatalf("%s + MaxAttempts=1 应首次失败即落终态，不该重排", outcome)
					}
					if got := st.finished["t1"]; got != corestatus.TaskFailed {
						t.Fatalf("终态 = %q, want failed", got)
					}
					if signer.signs != 1 {
						t.Fatalf("sign 次数 = %d, want 1", signer.signs)
					}
					return
				}
				runAt, ok := st.requeued["t1"]
				if !ok {
					t.Fatalf("%s + MaxAttempts=2 应重试一次（宿主预算 3 被压到 2）", outcome)
				}
				// 第二次认领：只许再失败一次，然后落 failed。
				clk.t = runAt.Add(time.Second)
				if _, err := svc.Step(context.Background(), 10); err != nil {
					t.Fatalf("重试 Step: %v", err)
				}
				if signer.signs != 2 {
					t.Fatalf("sign 次数 = %d, want 2（MaxAttempts=2 ⇒ 只重试一次）", signer.signs)
				}
				if got := st.finished["t1"]; got != corestatus.TaskFailed {
					t.Fatalf("重试后终态 = %q, want failed", got)
				}
				if signer.attemptIDs[0] == signer.attemptIDs[1] {
					t.Fatalf("两次幂等键相同 = %q（重试必须换键）", signer.attemptIDs[0])
				}
			})
		}
	}
}

// 退避落点以结算时刻（settleAt）为基准，不用尝试开始时刻：
// 一次尝试整段耗在预算里时（缺省 0.8×租约 ≈ 96s），用开始时刻算会把
// run_at 写进过去，下一次 tick 立刻再撞一次平台。
//
// 这里用可进时钟模拟"整段耗在预算里"，而不是真的烧预算：真实预算耗时
// （毫秒级）远小于退避下限（30s 起），两种基准算出的落点区分不开。
func TestBackoffAnchoredOnSettleTimeAfterLongAttempt(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	signer.outcome = contract.OutcomeTransient
	signer.retry = contract.RetryAdvice{Retryable: true, MaxAttempts: 3, After: 90 * time.Second}
	clk := &stepClock{t: testNow()}
	signer.onSign = func() { clk.t = clk.t.Add(5 * time.Minute) }
	svc := newServiceWithClock(t, st, signer, fakeLimiter{allow: true}, clk)

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	runAt, ok := st.requeued["t1"]
	if !ok {
		t.Fatal("预算内应重排重试")
	}
	settleAt := testNow().Add(5 * time.Minute) // 尝试耗掉 5 分钟后才结算
	if !runAt.After(settleAt) {
		t.Fatalf("落点 = %v 早于结算时刻 %v（用尝试开始时刻算会落进过去）", runAt, settleAt)
	}
	low, high := settleAt.Add(90*time.Second), settleAt.Add(136*time.Second)
	if runAt.Before(low) || runAt.After(high) {
		t.Fatalf("落点 = %v, want [%v,%v]（settleAt + 90s 下限，抖动 [90s,135s]）", runAt, low, high)
	}
	if got := st.tasks[0].Attempts; got != 1 {
		t.Fatalf("attempts = %d, want 1（失败一次计一次）", got)
	}
	if got := st.results["t1"]; got != string(contract.OutcomeTransient) {
		t.Fatalf("result = %q, want transient", got)
	}
}
