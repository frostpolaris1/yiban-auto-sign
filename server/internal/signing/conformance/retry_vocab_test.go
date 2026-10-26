package conformance

import (
	"testing"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/retry"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// 词表一致性（P4-A 复审 P2-1 的门）：retry.BudgetFor 的分级必须覆盖
// contract 的全部 Outcome 常量。判据吃 **contract 真源**（遍历常量本身，
// 不是字面量副本）：契约改一个词名，本用例立刻红——词表漂移无处遁形。
// core/retry 不 import signing（单向依赖），故对照表放在合约层这一侧。
func TestRetryBudgetCoversContractOutcomes(t *testing.T) {
	outcomes := []contract.Outcome{
		contract.OutcomeSuccess,
		contract.OutcomeAlreadyDone,
		contract.OutcomePendingUpstream,
		contract.OutcomeNoTarget,
		contract.OutcomeInvalidTarget,
		contract.OutcomeCredentialBad,
		contract.OutcomeDeviceUnbound,
		contract.OutcomeChallenge,
		contract.OutcomeTransient,
		contract.OutcomeRejected,
		contract.OutcomePhotoMissing,
		contract.OutcomePhotoRejected,
		contract.OutcomeUnknown,
	}
	// default 档形状（未知词分支）：1 次＋清会话。任何已知词落到它都算漂移——
	// 每个词都必须有自己的显式分支。
	for _, o := range outcomes {
		p := retry.BudgetFor(string(o))
		if p.MaxAttempts == 1 && p.ClearSession {
			// challenge 与 credential_invalid 是**合法**的 1 次＋清会话；
			// 其余词落到该形状只能是 default 档（词缺分支）。
			if o != contract.OutcomeChallenge && o != contract.OutcomeCredentialBad {
				t.Fatalf("Outcome %q 落进 default 档：BudgetFor 缺该词分支", o)
			}
		}
		// 已了结类必须零预算：绝不为完成态排重试。
		switch o {
		case contract.OutcomeSuccess, contract.OutcomeAlreadyDone,
			contract.OutcomeNoTarget, contract.OutcomePhotoMissing:
			if p.MaxAttempts != 0 {
				t.Fatalf("Outcome %q 应零预算, got %+v", o, p)
			}
		}
		// 确定类必须清会话：会话已不可信。
		if o == contract.OutcomeChallenge || o == contract.OutcomeCredentialBad {
			if !p.ClearSession {
				t.Fatalf("Outcome %q 应清会话, got %+v", o, p)
			}
		}
		// 瞬时类必须 3 次：给网络抖动留重试空间。
		if o == contract.OutcomeTransient || o == contract.OutcomePendingUpstream ||
			o == contract.OutcomeUnknown {
			if p.MaxAttempts != 3 {
				t.Fatalf("Outcome %q 应 3 次预算, got %+v", o, p)
			}
		}
	}
}
