package yiban

// 发现路径的会话失效（OCR M2）：
// 判据链：protocol/envelopes.go 把 999 变为 SessionExpired；
// client 重登一次仍 999 则上抛；provider 的发现面必须转成可行动的结论。
// 不转换时宿主只看到未分类错误，日志也没有线索。

import (
	"context"
	"errors"
	"testing"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// 上游回 999：窗口与候选点两条发现入口都给 transient 与固定文案。
func TestProviderDiscoverySessionExpired(t *testing.T) {
	cases := []struct {
		name string
		call func(*Provider) error
	}{
		{"窗口", func(p *Provider) error {
			_, err := p.DiscoverWindow(context.Background(), contract.DiscoverRequest{
				Account: providerAccount(), Credential: providerCredential()})
			return err
		}},
		{"候选点", func(p *Provider) error {
			_, err := p.DiscoverTargets(context.Background(), contract.DiscoverRequest{
				Account: providerAccount(), Credential: providerCredential()})
			return err
		}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			p, up := newTestProvider(t)
			up.mu.Lock()
			up.signPositionBody = []byte(`{"code":999,"msg":"expired"}`)
			up.mu.Unlock()
			p.client.clearPositions("acct-1")

			err := c.call(p)
			var outcomeErr *contract.OutcomeError
			if !errors.As(err, &outcomeErr) {
				t.Fatalf("error = %v, want OutcomeError", err)
			}
			if outcomeErr.Outcome != contract.OutcomeTransient {
				t.Fatalf("outcome = %q, want transient", outcomeErr.Outcome)
			}
			if outcomeErr.Message != "session expired" {
				t.Fatalf("message = %q, want 固定文案 session expired", outcomeErr.Message)
			}
		})
	}
}
