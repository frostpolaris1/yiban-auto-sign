package schedule_test

// 窗口发现失败可诊断（OCR M1）：
// 计划层原先用 `err == nil &&` 静默吞掉发现错误，只回退不记录。
// 判据：发现失败仍按回退窗口生成计划（现行为不变）；
// 同时留一条日志，含结论词、原因与任务标识。

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"net/url"
	"strings"
	"testing"

	appschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schedule"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// errLeakyTransport 模拟传输错误：原文带完整 URL 与查询串（会话串）。
// 地址取 TEST-NET-1 段，形状明显是合成值。
var errLeakyTransport = &url.Error{
	Op:  "Get",
	URL: "https://192.0.2.1/nightAttendance/signPosition?CSRF=session-token-abc",
	Err: errors.New("dial tcp 192.0.2.1:443: connect: connection refused"),
}

// 用户动作：平台窗口发现返回未知 State，管理员预览当日计划。
// 用户可见结果：计划仍按回退窗口生成；日志能查到失败原因。
func TestWindowDiscoveryFailureIsLogged(t *testing.T) {
	const reason = "platform state 7 is not a known state"
	var buf bytes.Buffer
	svc, err := appschedule.NewService(appschedule.Deps{
		Windows:  &fakeWindowStore{},
		Plans:    &fakePlanStore{},
		Accounts: fakeAccounts{ids: []string{"acct-1"}},
		Discover: &fakeDiscover{err: &contract.OutcomeError{
			Outcome: contract.OutcomeUnknown, Message: reason}},
		Tx:     &fakeTx{},
		Logger: slog.New(slog.NewTextHandler(&buf, nil)),
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}

	p, err := svc.Preview(context.Background(), baseInput())
	if err != nil {
		t.Fatalf("Preview: %v", err)
	}
	if p.Source != contract.WindowFallback {
		t.Fatalf("Source = %q, want fallback（发现失败仍回退）", p.Source)
	}
	logged := buf.String()
	for _, want := range []string{reason, string(contract.OutcomeUnknown), "task-1"} {
		if !strings.Contains(logged, want) {
			t.Fatalf("日志缺少 %q: %s", want, logged)
		}
	}
}

// 发现返回非结论错误（网络/解析）时同样留日志：否则回退同样无迹可查。
// 传输错误的原文带 URL 与查询串，日志只留分类词。
func TestWindowDiscoveryPlainErrorIsLogged(t *testing.T) {
	var buf bytes.Buffer
	svc, err := appschedule.NewService(appschedule.Deps{
		Windows:  &fakeWindowStore{},
		Plans:    &fakePlanStore{},
		Accounts: fakeAccounts{ids: []string{"acct-1"}},
		Discover: &fakeDiscover{err: errLeakyTransport},
		Tx:       &fakeTx{},
		Logger:   slog.New(slog.NewTextHandler(&buf, nil)),
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	if _, err := svc.Preview(context.Background(), baseInput()); err != nil {
		t.Fatalf("Preview: %v", err)
	}
	logged := buf.String()
	if !strings.Contains(logged, "transport error") {
		t.Fatalf("日志缺少错误分类词: %s", logged)
	}
	for _, leak := range []string{"session-token-abc", "CSRF="} {
		if strings.Contains(logged, leak) {
			t.Fatalf("日志泄漏会话串 %q: %s", leak, logged)
		}
	}
}

// 日志级别按结论分类：已签到这类正常结论 Info，未知结论 Warn。
// 与 worker 同一口径：normal 结论不得进按级别告警的运维面。
func TestWindowDiscoveryLevelByOutcome(t *testing.T) {
	cases := []struct {
		name    string
		outcome contract.Outcome
		want    string
	}{
		{"已签到", contract.OutcomeAlreadyDone, "level=INFO"},
		{"无需签到", contract.OutcomeNoTarget, "level=INFO"},
		{"未知", contract.OutcomeUnknown, "level=WARN"},
		{"瞬时", contract.OutcomeTransient, "level=WARN"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			var buf bytes.Buffer
			svc, err := appschedule.NewService(appschedule.Deps{
				Windows:  &fakeWindowStore{},
				Plans:    &fakePlanStore{},
				Accounts: fakeAccounts{ids: []string{"acct-1"}},
				Discover: &fakeDiscover{err: &contract.OutcomeError{
					Outcome: c.outcome, Message: "platform message"}},
				Tx:     &fakeTx{},
				Logger: slog.New(slog.NewTextHandler(&buf, nil)),
			})
			if err != nil {
				t.Fatalf("NewService: %v", err)
			}
			if _, err := svc.Preview(context.Background(), baseInput()); err != nil {
				t.Fatalf("Preview: %v", err)
			}
			if !strings.Contains(buf.String(), c.want) {
				t.Fatalf("级别 = %s, want %s", buf.String(), c.want)
			}
		})
	}
}
