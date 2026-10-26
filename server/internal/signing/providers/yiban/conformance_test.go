package yiban

import (
	"testing"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/conformance"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// successExpecter 让符合性套件的并发判据能关联 AttemptID。
// 假上游对每次签到都回成功，故期望结果恒为 success。
type successExpecter struct{ *Provider }

func (p *successExpecter) ExpectedOutcome(contract.SignRequest) (contract.Outcome, bool) {
	return contract.OutcomeSuccess, true
}

// TestProviderConformanceApplicable 跑真实 Provider 的适用判据。
// sign.timeout 依赖假上游的延迟脚本；sign.photo_missing 属 P5。
func TestProviderConformanceApplicable(t *testing.T) {
	up := newUpstream(t)
	newProvider := func() contract.Provider {
		return &successExpecter{Provider: NewProvider(New(Config{Endpoints: up.endpoints()}))}
	}
	skip := map[string]string{
		"sign.timeout":       "假上游没有延迟脚本；HTTP 超时由 sign.cancel 与客户端覆盖",
		"sign.photo_missing": "ImageRequired 在 P5 前为假",
	}
	for _, check := range conformance.Checks() {
		check := check
		if reason, ok := skip[check.Name]; ok {
			t.Logf("跳过 %s：%s", check.Name, reason)
			continue
		}
		t.Run(check.Name, func(t *testing.T) {
			if err := check.Run(newProvider); err != nil {
				t.Fatalf("%v", err)
			}
		})
	}
}
