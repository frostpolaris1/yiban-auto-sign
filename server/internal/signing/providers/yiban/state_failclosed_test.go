package yiban

// State 未知值 fail-closed（工单 mks）。
//
// 判据：平台返回未登记的 State 时，提供方不得把它当作可签。
// 三条入口（窗口、候选点、签到）一律给 unknown，签到接口零请求。
// 边界：已登记态不得被收紧——0（可签到）与 1（未签到）照常签到，
// 2/3/4/5 各自的结论由既有测试钉住。

import (
	"context"
	"encoding/json"
	"errors"
	"testing"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// setSignPositionStateValue 写入任意 State 值。
// value 为 nil 时删除该键，模拟平台缺字段。
func (u *upstream) setSignPositionStateValue(t *testing.T, value any) {
	t.Helper()
	var obj map[string]any
	if err := json.Unmarshal(u.signPositionFixture, &obj); err != nil {
		t.Fatalf("unmarshal signPosition: %v", err)
	}
	data, ok := obj["data"].(map[string]any)
	if !ok {
		t.Fatal("signPosition data is not an object")
	}
	if value == nil {
		delete(data, "State")
	} else {
		data["State"] = value
	}
	body, err := json.Marshal(obj)
	if err != nil {
		t.Fatalf("marshal signPosition: %v", err)
	}
	u.mu.Lock()
	u.signPositionBody = body
	u.mu.Unlock()
}

// isUnknownOutcome 报告 err 是否为 unknown 结论错误。
func isUnknownOutcome(err error) bool {
	var outcomeErr *contract.OutcomeError
	return errors.As(err, &outcomeErr) && outcomeErr.Outcome == contract.OutcomeUnknown
}

// 用户动作：平台给出未登记的 State，账号到点执行签到。
// 用户可见结果：不发出签到请求，结论是 unknown。
func TestProviderUnknownStateIsNeverSignable(t *testing.T) {
	cases := []struct {
		name  string
		state any
	}{
		{"未登记正数", 7},
		{"负数", -1},
		{"缺失键", nil},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			p, up := newTestProvider(t)
			up.setSignPositionStateValue(t, tc.state)
			p.client.clearPositions("acct-1")
			req := contract.DiscoverRequest{Account: providerAccount(), Credential: providerCredential()}
			ctx := context.Background()

			if _, err := p.DiscoverWindow(ctx, req); !isUnknownOutcome(err) {
				t.Fatalf("DiscoverWindow error = %v, want unknown", err)
			}
			if _, err := p.DiscoverTargets(ctx, req); !isUnknownOutcome(err) {
				t.Fatalf("DiscoverTargets error = %v, want unknown", err)
			}
			result, err := p.Sign(ctx, providerSignRequest("attempt-unknown-state"))
			if err != nil {
				t.Fatalf("Sign: %v", err)
			}
			if result.Outcome != contract.OutcomeUnknown {
				t.Fatalf("outcome = %q, want unknown", result.Outcome)
			}
			if got := up.count("/nightAttendance/student/index/signIn"); got != 0 {
				t.Fatalf("signIn calls = %d, want 0", got)
			}
		})
	}
}

// 边界：State=1（未签到）是已登记态，必须照常签到。
// 把它误收紧成 unknown 会让这类账号当日不再签到。
func TestProviderStateOneStaysSignable(t *testing.T) {
	p, up := newTestProvider(t)
	up.setSignPositionStateValue(t, 1)
	p.client.clearPositions("acct-1")
	req := contract.DiscoverRequest{Account: providerAccount(), Credential: providerCredential()}
	ctx := context.Background()

	if _, err := p.DiscoverWindow(ctx, req); err != nil {
		t.Fatalf("DiscoverWindow: %v", err)
	}
	targets, err := p.DiscoverTargets(ctx, req)
	if err != nil || len(targets) == 0 {
		t.Fatalf("DiscoverTargets = (%d,%v), want non-empty", len(targets), err)
	}
	result, err := p.Sign(ctx, providerSignRequest("attempt-state-one"))
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	if result.Outcome != contract.OutcomeSuccess {
		t.Fatalf("outcome = %q, want success", result.Outcome)
	}
	if got := up.count("/nightAttendance/student/index/signIn"); got != 1 {
		t.Fatalf("signIn calls = %d, want 1", got)
	}
}
