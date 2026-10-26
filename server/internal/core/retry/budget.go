package retry

// 重试预算与退避落点。
//
// 这组判据把一次失败结论（contract.Outcome）翻译成分级预算：
// 瞬时类多试、确定类少试、已了结不试；退避落点受窗口约束：
// 绝不让重试越过窗口终点，窗口尾段放不下一个合法间隔时放弃。
// 预算的词表以 internal/signing/contract 的 Outcome 为准，
// 本包不另造一套结论词。

import "time"

// 尾段份额：退避落点可从窗口剩余部分拿到的最大比例。
// 落点公式与预算判据必须用同一个数，否则两处静默漂移。
const TailRatio = 0.3

// CapSec 是退避落点的硬上限（秒）：安全类参数，恒为常量。
const CapSec = 600 * time.Second

// BudgetFor 把一次失败结论翻译成分级预算。
//   - transient/pending_upstream/unknown：最多 3 次（1 初始 + 2 重试）；
//   - challenge/credential_invalid/device_unbound/invalid_target/rejected/
//     photo_rejected：仅 1 次——确定类失败重试只会重发同一死请求，
//     且前两类还会加重账号标记；
//   - 已了结类（success/already_done/no_target/photo_missing）：0 次；
//   - 词表外的结论：按最保守档（1 次＋清会话）——未知结论绝不按可重试处理，
//     也绝不清空预算（unknown 与 success 必须保持区分）。
func BudgetFor(outcome string) Policy {
	switch outcome {
	case "transient", "pending_upstream", "unknown":
		return Policy{MaxAttempts: 3, After: DefaultAfter, ClearSession: false}
	case "challenge", "credential_invalid":
		return Policy{MaxAttempts: 1, After: DefaultAfter, ClearSession: true}
	case "device_unbound", "invalid_target", "rejected", "photo_rejected":
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

// NextDelay 返回下一次重试的等待时长：(delay, true)；放弃返回 (0, false)。
//
// delay = min(cap, max(base, last×3))，cap = min(CapSec, 剩余×TailRatio)。
// cap < base 时放弃：窗口尾段放不下一个合法间隔，宁可当日放弃，
// 也不把间隔压到基础值之下。窗口已过（剩余 ≤ 0）同样放弃：
// 业务日窗口已过 ⇒ 绝不把落点写进次日窗口。
func NextDelay(p Policy, last time.Duration, w Window, now time.Time) (time.Duration, bool) {
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
	delay := time.Duration(float64(last) * 3)
	if delay < base {
		delay = base
	}
	if delay > cap {
		delay = cap
	}
	return delay, true
}
