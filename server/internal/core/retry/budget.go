package retry

// 重试预算与退避落点。
//
// 这组判据把一次失败结论（contract.Outcome）翻译成分级预算：
// 瞬时类多试、确定类少试、已了结不试；退避落点受窗口约束：
// 绝不让重试越过窗口终点，窗口尾段放不下一个合法间隔时放弃。
// 预算的词表以 internal/signing/contract 的 Outcome 为准，
// 本包不另造一套结论词。退避曲线取 cenkalti/backoff 的指数＋抖动，
// 重试的**执行**仍由调度认领完成：本包只给等待时长。

import (
	"time"

	"github.com/cenkalti/backoff/v5"
)

// 尾段份额：退避落点可从窗口剩余部分拿到的最大比例。
// 落点公式与预算判据必须用同一个数，否则两处静默漂移。
const TailRatio = 0.3

// CapSec 是退避落点的硬上限（秒）：安全类参数，恒为常量。
const CapSec = 600 * time.Second

// 退避曲线参数：指数倍数与抖动幅度。
//
// 抖动的作用是打散同批失败的重试落点：同一秒集中重发等于又一次连击。
const (
	// Multiplier 是相邻两档的间隔倍数：base、2×base、4×base……
	Multiplier = 2.0
	// RandomizationFactor 是抖动幅度：每档取 [1-f, 1+f] 的随机倍率。
	RandomizationFactor = 0.5
)

// BudgetFor 把一次失败结论翻译成分级预算。
//   - transient/pending_upstream/unknown：最多 3 次（1 初始 + 2 重试）；
//   - challenge/credential_invalid：仅 1 次，且清会话；
//   - blocked/device_unbound/invalid_target/rejected/photo_rejected：
//     仅 1 次——确定类失败重试只会重发同一死请求；
//     blocked 是风控拦截，重发还会加深账号标记；
//   - 已了结类（success/already_done/no_target/photo_missing）：0 次；
//   - 词表外的结论：按最保守档（1 次＋清会话）——未知结论绝不按可重试处理，
//     也绝不清空预算（unknown 与 success 必须保持区分）。
func BudgetFor(outcome string) Policy {
	switch outcome {
	case "transient", "pending_upstream", "unknown":
		return Policy{MaxAttempts: 3, After: DefaultAfter, ClearSession: false}
	case "challenge", "credential_invalid":
		return Policy{MaxAttempts: 1, After: DefaultAfter, ClearSession: true}
	case "blocked", "device_unbound", "invalid_target", "rejected", "photo_rejected":
		return Policy{MaxAttempts: 1, After: DefaultAfter, ClearSession: false}
	case "success", "already_done", "no_target", "photo_missing":
		return Policy{MaxAttempts: 0, After: 0, ClearSession: false}
	default:
		return Policy{MaxAttempts: 1, After: DefaultAfter, ClearSession: true}
	}
}

// Window 是退避落点用的窗口形状：只需终点。
// 与 plan.Window 分开：retry 不关心片几何。
type Window struct {
	End time.Time
}

// NextDelay 返回第 attempt 次失败后的等待时长：(delay, true)；放弃返回 (0, false)。
//
// 曲线由 cenkalti/backoff 计算：第 attempt 档的名义间隔 = base×Multiplier^(attempt-1)，
// 再乘 [1-RandomizationFactor, 1+RandomizationFactor] 的随机倍率。
// 本包只在曲线上加一条窗口约束：cap = min(CapSec, 剩余×TailRatio)，
// delay = min(曲线值, cap)。**只有曲线外置，窗口判据仍在本包**——
// 退避不得越过窗口终点是调度判据，不是通用重试细节。
// cap < base 时放弃：窗口尾段放不下一个合法间隔，宁可当日放弃，
// 也不把间隔压到基础值之下。窗口已过（剩余 ≤ 0）同样放弃：
// 业务日窗口已过 ⇒ 绝不把落点写进次日窗口。
func NextDelay(p Policy, attempt int, w Window, now time.Time) (time.Duration, bool) {
	remaining := w.End.Sub(now)
	if remaining <= 0 {
		return 0, false
	}
	base := p.After
	if base <= 0 {
		base = DefaultAfter
	}
	cap := CapSec
	if tail := time.Duration(float64(remaining) * TailRatio); tail < cap {
		cap = tail
	}
	if cap < base {
		return 0, false
	}
	delay := jittered(p, attempt, base)
	if delay > cap {
		delay = cap
	}
	return delay, true
}

// jittered 取第 attempt 档（1 起）的抖动间隔。
//
// 每次现建曲线实例：backoff.ExponentialBackOff 不是并发安全的，
// 而结算可能在多个执行协程里同时发生（批内并行，见 worker.Step）。
func jittered(p Policy, attempt int, base time.Duration) time.Duration {
	if attempt < 1 {
		attempt = 1
	}
	curve := &backoff.ExponentialBackOff{
		InitialInterval:     base,
		RandomizationFactor: RandomizationFactor,
		Multiplier:          Multiplier,
		MaxInterval:         CapSec,
	}
	var delay time.Duration
	for i := 0; i < attempt; i++ {
		delay = curve.NextBackOff()
	}
	if delay <= 0 {
		// 曲线不给合法值时不退回零等待：基础间隔是最后一道兜底。
		return base
	}
	return delay
}
