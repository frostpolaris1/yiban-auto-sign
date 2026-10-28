package worker_test

// 结论原因可诊断（OCR M1）：
// worker 原先只取结论词、丢弃提供方给的 message，库里只剩一个词。
// 判据：每条结论都留一条结构化日志，含结论词与原因；
// result 词表不变，仍是 contract.Outcome 的词面值。

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/url"
	"strings"
	"sync"
	"testing"

	appworker "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/worker"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// logSink 收集 JSON 日志行，供断言使用。
type logSink struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (s *logSink) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.buf.Write(p)
}

// records 返回已写入的日志记录。
func (s *logSink) records(t *testing.T) []map[string]any {
	t.Helper()
	s.mu.Lock()
	text := s.buf.String()
	s.mu.Unlock()
	var out []map[string]any
	for _, line := range strings.Split(strings.TrimSpace(text), "\n") {
		if line == "" {
			continue
		}
		var rec map[string]any
		if err := json.Unmarshal([]byte(line), &rec); err != nil {
			t.Fatalf("日志行不是 JSON: %q", line)
		}
		out = append(out, rec)
	}
	return out
}

// hasReason 报告是否存在指定结论词与原因的日志。
func hasReason(records []map[string]any, outcome, reason string) bool {
	for _, rec := range records {
		if rec["outcome"] == outcome && rec["reason"] == reason {
			return true
		}
	}
	return false
}

// levelOf 返回指定结论词那条日志的级别；找不到返回空串。
func levelOf(records []map[string]any, outcome string) string {
	for _, rec := range records {
		if rec["outcome"] == outcome {
			level, _ := rec["level"].(string)
			return level
		}
	}
	return ""
}

// scriptedSigner 按脚本返回发现与签到结果；两条入口可分别注入错误。
type scriptedSigner struct {
	targets     []contract.Target
	discoverErr error
	signResult  contract.Result
	signErr     error
}

func (s *scriptedSigner) DiscoverTargets(context.Context, contract.DiscoverRequest) ([]contract.Target, error) {
	if s.discoverErr != nil {
		return nil, s.discoverErr
	}
	return s.targets, nil
}

func (s *scriptedSigner) Sign(context.Context, contract.SignRequest) (contract.Result, error) {
	return s.signResult, s.signErr
}

func newReasonService(t *testing.T, st *fakeStore, signer appworker.Signer, sink *logSink) *appworker.Service {
	t.Helper()
	svc, err := appworker.NewService(appworker.Deps{
		Store: st, Creds: fakeCreds{}, Signer: signer, Egress: fakeEgress{},
		Limiter: fakeLimiter{allow: true}, Clock: coreclock.Fixed{T: testNow()}, Location: loc,
		Logger: slog.New(slog.NewJSONHandler(sink, nil)),
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

// 用户动作：平台返回未知 State，账号到点执行签到。
// 用户可见结果：任务结果仍是词表里的 unknown；日志能查到原因。
func TestUnknownOutcomeLeavesDiagnosableReason(t *testing.T) {
	const reason = "platform state 7 is not a known state"
	cases := []struct {
		name   string
		signer *scriptedSigner
	}{
		{"发现阶段错误", &scriptedSigner{discoverErr: &contract.OutcomeError{
			Outcome: contract.OutcomeUnknown, Message: reason}}},
		{"提交阶段错误", &scriptedSigner{
			targets: []contract.Target{{ID: "pos-1"}},
			signErr: &contract.OutcomeError{Outcome: contract.OutcomeUnknown, Message: reason}}},
		{"提交阶段结论", &scriptedSigner{
			targets:    []contract.Target{{ID: "pos-1"}},
			signResult: contract.Result{Outcome: contract.OutcomeUnknown, Message: reason}}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			st := newStore(baseTask())
			st.windows["st-1|2026-10-29"] = openWindow()
			sink := &logSink{}
			svc := newReasonService(t, st, c.signer, sink)

			if _, err := svc.Step(context.Background(), 10); err != nil {
				t.Fatalf("Step: %v", err)
			}
			if got := st.results["t1"]; got != string(contract.OutcomeUnknown) {
				t.Fatalf("result = %q, want %q", got, contract.OutcomeUnknown)
			}
			if !hasReason(sink.records(t), string(contract.OutcomeUnknown), reason) {
				t.Fatalf("日志中没有结论词与原因: %v", sink.records(t))
			}
		})
	}
}

// 未知结论即使没有原因文本，也必须留一条日志。
// 这样"提供方没给原因"本身可见，不会静默。
func TestUnknownWithoutMessageStillLogs(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	sink := &logSink{}
	signer := &scriptedSigner{
		targets:    []contract.Target{{ID: "pos-1"}},
		signResult: contract.Result{Outcome: contract.OutcomeUnknown},
	}
	svc := newReasonService(t, st, signer, sink)

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	found := false
	for _, rec := range sink.records(t) {
		if rec["outcome"] == string(contract.OutcomeUnknown) {
			found = true
		}
	}
	if !found {
		t.Fatalf("无原因的 unknown 未留日志: %v", sink.records(t))
	}
}

// 结论原因进日志前必须掩码：手机号不得原样出现在日志里。
func TestOutcomeReasonIsMasked(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	sink := &logSink{}
	signer := &scriptedSigner{
		targets:    []contract.Target{{ID: "pos-1"}},
		signResult: contract.Result{Outcome: contract.OutcomeRejected, Message: "call 13912345678 to appeal"},
	}
	svc := newReasonService(t, st, signer, sink)

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	records := sink.records(t)
	if !hasReason(records, string(contract.OutcomeRejected), "call 139****5678 to appeal") {
		t.Fatalf("日志原因未按口径掩码: %v", records)
	}
}

// 传输错误的原文带完整 URL 与查询串，日志只留分类词。
// 合成地址取 TEST-NET-1 段；会话串是固定假值。
func TestTransportErrorDoesNotLeakURL(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	sink := &logSink{}
	signer := &scriptedSigner{
		discoverErr: &url.Error{
			Op:  "Get",
			URL: "https://192.0.2.1/nightAttendance/signPosition?CSRF=session-token-abc",
			Err: errors.New("dial tcp 192.0.2.1:443: connect: connection refused"),
		},
	}
	svc := newReasonService(t, st, signer, sink)

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	records := sink.records(t)
	if !hasReason(records, string(contract.OutcomeTransient), "transport error") {
		t.Fatalf("日志缺少错误分类词: %v", records)
	}
	for _, rec := range records {
		line, err := json.Marshal(rec)
		if err != nil {
			t.Fatalf("日志记录无法编码: %v", err)
		}
		if strings.Contains(string(line), "session-token-abc") {
			t.Fatalf("日志泄漏会话串: %s", line)
		}
	}
}

// 日志级别按结论分类：正常结论 Info，需人看的结论 Warn。
// 一个学校 N 个已签账号每天 N 条 Warn 会污染按级别告警的运维面。
func TestOutcomeLogLevelByClass(t *testing.T) {
	cases := []struct {
		outcome contract.Outcome
		want    string
	}{
		{contract.OutcomeSuccess, "INFO"},
		{contract.OutcomeAlreadyDone, "INFO"},
		{contract.OutcomeNoTarget, "INFO"},
		{contract.OutcomePendingUpstream, "INFO"},
		{contract.OutcomeTransient, "WARN"},
		{contract.OutcomeUnknown, "WARN"},
		{contract.OutcomeChallenge, "WARN"},
		{contract.OutcomeRejected, "WARN"},
		{contract.OutcomeDeviceUnbound, "WARN"},
	}
	for _, c := range cases {
		t.Run(string(c.outcome), func(t *testing.T) {
			st := newStore(baseTask())
			st.windows["st-1|2026-10-29"] = openWindow()
			sink := &logSink{}
			signer := &scriptedSigner{
				targets:    []contract.Target{{ID: "pos-1"}},
				signResult: contract.Result{Outcome: c.outcome, Message: "platform message"},
			}
			svc := newReasonService(t, st, signer, sink)
			if _, err := svc.Step(context.Background(), 10); err != nil {
				t.Fatalf("Step: %v", err)
			}
			if got := levelOf(sink.records(t), string(c.outcome)); got != c.want {
				t.Fatalf("%s 级别 = %q, want %q（记录 %v）", c.outcome, got, c.want, sink.records(t))
			}
		})
	}
}

// 会话失效的发现结论必须留可行动原因，不得落成 unclassified。
func TestSessionExpiredDiscoveryLogsFixedReason(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	sink := &logSink{}
	signer := &scriptedSigner{discoverErr: &contract.OutcomeError{
		Outcome: contract.OutcomeTransient, Message: "session expired"}}
	svc := newReasonService(t, st, signer, sink)

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	records := sink.records(t)
	if !hasReason(records, string(contract.OutcomeTransient), "session expired") {
		t.Fatalf("日志缺少 session expired: %v", records)
	}
	for _, rec := range records {
		if rec["reason"] == "unclassified error" {
			t.Fatalf("会话失效落成未分类错误: %v", rec)
		}
	}
}

// unknown 日志带 stage：无 message 的 unknown 要能区分发现面与提交面。
func TestUnknownLogCarriesStage(t *testing.T) {
	cases := []struct {
		name   string
		signer *scriptedSigner
		want   string
	}{
		{"发现面", &scriptedSigner{discoverErr: &contract.OutcomeError{
			Outcome: contract.OutcomeUnknown}}, "discovery"},
		{"提交面", &scriptedSigner{
			targets:    []contract.Target{{ID: "pos-1"}},
			signResult: contract.Result{Outcome: contract.OutcomeUnknown}}, "submit"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			st := newStore(baseTask())
			st.windows["st-1|2026-10-29"] = openWindow()
			sink := &logSink{}
			svc := newReasonService(t, st, c.signer, sink)
			if _, err := svc.Step(context.Background(), 10); err != nil {
				t.Fatalf("Step: %v", err)
			}
			found := false
			for _, rec := range sink.records(t) {
				if rec["outcome"] == string(contract.OutcomeUnknown) && rec["stage"] == c.want {
					found = true
				}
			}
			if !found {
				t.Fatalf("unknown 日志缺少 stage=%q: %v", c.want, sink.records(t))
			}
		})
	}
}
