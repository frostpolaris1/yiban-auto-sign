package yiban

// WAF 拦截的结论分类（C2）：
// 挑战形态 → challenge；仅关键词命中 → blocked。
// 两者都不可重试；重复请求只会加深风控标记。

import (
	"context"
	"errors"
	"net/http"
	"testing"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

func TestBlockedOutcomeClassification(t *testing.T) {
	cases := []struct {
		name string
		body string
		want contract.Outcome
	}{
		{"挑战形态", doubleJSSetup + doubleJSEval, contract.OutcomeChallenge},
		{"中文关键词", `<html><body>访问被拦截</body></html>`, contract.OutcomeBlocked},
		{"ASCII 词元", `request blocked by WAF`, contract.OutcomeBlocked},
		// 平台响应的中文在 \uXXXX 转义里，解码后才命中关键词。
		{"转义关键词", `{"msg":"\u98ce\u63a7"}`, contract.OutcomeBlocked},
	}
	for _, c := range cases {
		var outcomeErr *contract.OutcomeError
		if err := blockedOutcome(c.body); !errors.As(err, &outcomeErr) {
			t.Fatalf("%s: error = %v, want OutcomeError", c.name, err)
		}
		if outcomeErr.Outcome != c.want {
			t.Fatalf("%s: outcome = %q, want %q", c.name, outcomeErr.Outcome, c.want)
		}
	}
}

// 提交面命中关键词：结论是 blocked，且提供方不标记可重试。
// 这条钉住"1 次尝试即终态"的上游一半；下游一半见
// app/worker 的 TestWAFBlockedFailsWithoutRetry。
func TestProviderSignWAFKeyword(t *testing.T) {
	up := newUpstream(t)
	up.signInBody = []byte(`<html><body>风险访问</body></html>`)
	p := NewProvider(New(Config{Endpoints: up.endpoints()}))
	result, err := p.Sign(context.Background(), providerSignRequest("attempt-waf"))
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	if result.Outcome != contract.OutcomeBlocked || result.Retry.Retryable {
		t.Fatalf("result = %+v, want blocked 且不可重试", result)
	}
}

// 拦截页带 4xx：WAF 判定先于状态码，不得按 transient 排重试。
func TestProviderSignWAFKeywordOnErrorStatus(t *testing.T) {
	up := newUpstream(t)
	up.signInBody = []byte(`<html><body>访问被拦截</body></html>`)
	up.signInStatus = http.StatusForbidden
	p := NewProvider(New(Config{Endpoints: up.endpoints()}))
	result, err := p.Sign(context.Background(), providerSignRequest("attempt-waf-403"))
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	if result.Outcome != contract.OutcomeBlocked || result.Retry.Retryable {
		t.Fatalf("result = %+v, want blocked 且不可重试", result)
	}
}
