package worker_test

// 0v8 B 接线判据：结算落库成功后必须把事件交给通知端口。
//
// 覆盖：凭据错同时发管理员事件与用户失败邮件；确定性失败只发用户邮件；
// 成功与退避重排不发任何投递；端口为 nil 时现行为不变；
// 端口失败或 panic 不改结算结果与 Step 返回。
// 断言打真实调用点（Step → finishFailed / finishCredentialInvalid），
// 不直接调投递 helper。

import (
	"context"
	"errors"
	"log/slog"
	"strings"
	"testing"

	appworker "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/worker"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// recordingAlerts 记录端口收到的两类事件。可注入错误或 panic。
type recordingAlerts struct {
	events   []corenotify.Event
	failures []corenotify.UserFailure
	err      error
	panicNow bool
}

func (a *recordingAlerts) Dispatch(_ context.Context, event corenotify.Event) error {
	if a.panicNow {
		panic("alert dispatch panic")
	}
	a.events = append(a.events, event)
	return a.err
}

func (a *recordingAlerts) DispatchUserFailure(_ context.Context, failure corenotify.UserFailure) error {
	if a.panicNow {
		panic("alert dispatch panic")
	}
	a.failures = append(a.failures, failure)
	return a.err
}

// newAlertService 构造带通知端口的执行循环。
func newAlertService(t *testing.T, st *fakeStore, signer appworker.Signer, alerts appworker.Alerts) *appworker.Service {
	t.Helper()
	return newAlertServiceLogged(t, st, signer, alerts, nil)
}

// newAlertServiceLogged 允许用例注入 logger（panic 告警断言用）。
func newAlertServiceLogged(t *testing.T, st *fakeStore, signer appworker.Signer,
	alerts appworker.Alerts, logger *slog.Logger) *appworker.Service {
	t.Helper()
	svc, err := appworker.NewService(appworker.Deps{
		Store: st, Creds: fakeCreds{}, Signer: signer, Egress: fakeEgress{},
		Limiter: fakeLimiter{allow: true}, Clock: coreclock.Fixed{T: testNow()}, Location: loc,
		Alerts: alerts, Logger: logger,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

// 凭据错：管理员事件（带业务键）与用户本人失败邮件都要发。
func TestCredentialInvalidDispatchesAdminAndUserFailure(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := &scriptedSigner{
		targets: []contract.Target{{ID: "pos-1"}},
		signErr: &contract.OutcomeError{
			Outcome: contract.OutcomeCredentialBad, Message: "登录失败: 账号或密码错误"},
	}
	alerts := &recordingAlerts{}
	svc := newAlertService(t, st, signer, alerts)

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if got := st.finished["t1"]; got != corestatus.TaskFailed {
		t.Fatalf("state = %q, want failed", got)
	}
	if len(alerts.events) != 1 {
		t.Fatalf("管理员事件 = %d, want 1", len(alerts.events))
	}
	event := alerts.events[0]
	if event.Category != corenotify.CategoryCredentialInvalid ||
		event.SchoolID != "school-1" || event.AccountID != "acct-1" ||
		event.Day != "2026-10-29" ||
		event.DedupeKey != "credential_invalid/acct-1/2026-10-29" {
		t.Fatalf("事件 = %+v", event)
	}
	if len(alerts.failures) != 1 {
		t.Fatalf("用户失败邮件 = %d, want 1", len(alerts.failures))
	}
	failure := alerts.failures[0]
	if failure.AccountID != "acct-1" || failure.SchoolID != "school-1" ||
		failure.Day != "2026-10-29" || failure.Outcome != string(contract.OutcomeCredentialBad) ||
		failure.Reason != "登录失败: 账号或密码错误" {
		t.Fatalf("用户失败事件 = %+v", failure)
	}
}

// 确定性失败：只发用户本人邮件，不发管理员事件。
func TestFailedOutcomeDispatchesOnlyUserFailure(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := &scriptedSigner{
		targets:    []contract.Target{{ID: "pos-1"}},
		signResult: contract.Result{Outcome: contract.OutcomeBlocked, Message: "风控拦截"},
	}
	alerts := &recordingAlerts{}
	svc := newAlertService(t, st, signer, alerts)

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if len(alerts.events) != 0 {
		t.Fatalf("确定性失败不应发管理员事件，得到 %d 条", len(alerts.events))
	}
	if len(alerts.failures) != 1 || alerts.failures[0].Outcome != string(contract.OutcomeBlocked) {
		t.Fatalf("用户失败事件 = %+v", alerts.failures)
	}
}

// 成功与退避重排都不发投递：只有落 failed 的两条路径才发。
func TestSuccessAndRequeueDoNotDispatchAlerts(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	alerts := &recordingAlerts{}
	svc := newAlertService(t, st, defaultSigner(), alerts)

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if st.finished["t1"] != corestatus.TaskSuccess {
		t.Fatalf("state = %q, want success", st.finished["t1"])
	}
	if len(alerts.events) != 0 || len(alerts.failures) != 0 {
		t.Fatalf("成功路径不应投递：events=%d failures=%d", len(alerts.events), len(alerts.failures))
	}
}

// 端口为 nil：结算行为与返回完全不变。
func TestNilAlertsKeepsSettlementBehavior(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := &scriptedSigner{
		targets:    []contract.Target{{ID: "pos-1"}},
		signResult: contract.Result{Outcome: contract.OutcomeBlocked, Message: "风控拦截"},
	}
	svc := newAlertService(t, st, signer, nil)

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if got := st.finished["t1"]; got != corestatus.TaskFailed {
		t.Fatalf("state = %q, want failed", got)
	}
}

// 端口失败或 panic 不改结算：Step 仍返回 nil，终态仍是 failed。
func TestAlertFailureDoesNotChangeSettlement(t *testing.T) {
	for _, tc := range []struct {
		name   string
		alerts *recordingAlerts
	}{
		{"错误", &recordingAlerts{err: errors.New("dispatch failed")}},
		{"panic", &recordingAlerts{panicNow: true}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			st := newStore(baseTask())
			st.windows["st-1|2026-10-29"] = openWindow()
			signer := &scriptedSigner{
				targets: []contract.Target{{ID: "pos-1"}},
				signErr: &contract.OutcomeError{
					Outcome: contract.OutcomeCredentialBad, Message: "登录失败"},
			}
			svc := newAlertService(t, st, signer, tc.alerts)

			if _, err := svc.Step(context.Background(), 10); err != nil {
				t.Fatalf("Step 不得因投递失败返回错误: %v", err)
			}
			if got := st.finished["t1"]; got != corestatus.TaskFailed {
				t.Fatalf("state = %q, want failed", got)
			}
		})
	}
}

// 退避重排不是终态：transient 走 requeueRetry，不发任何投递。
func TestTransientRequeueDoesNotDispatchAlerts(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := &scriptedSigner{
		targets:    []contract.Target{{ID: "pos-1"}},
		signResult: contract.Result{Outcome: contract.OutcomeTransient, Message: "网络抖动"},
	}
	alerts := &recordingAlerts{}
	svc := newAlertService(t, st, signer, alerts)

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if _, requeued := st.requeued["t1"]; !requeued {
		t.Fatalf("transient 未走退避重排：requeued=%v finished=%v",
			st.requeued, st.finished)
	}
	if len(alerts.events) != 0 || len(alerts.failures) != 0 {
		t.Fatalf("退避重排不应投递：events=%d failures=%d", len(alerts.events), len(alerts.failures))
	}
}

// 投递端口 panic 要留一条不含 panic 值的显性告警。
func TestAlertPanicIsLogged(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := &scriptedSigner{
		targets: []contract.Target{{ID: "pos-1"}},
		signErr: &contract.OutcomeError{
			Outcome: contract.OutcomeCredentialBad, Message: "登录失败"},
	}
	sink := &logSink{}
	svc := newAlertServiceLogged(t, st, signer, &recordingAlerts{panicNow: true},
		slog.New(slog.NewJSONHandler(sink, nil)))

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	var found bool
	for _, rec := range sink.records(t) {
		if rec["event"] == "notify.dispatch_panic" {
			found = true
			if strings.Contains(rec["msg"].(string), "alert dispatch panic") {
				t.Fatal("日志复述了 panic 值")
			}
		}
	}
	if !found {
		t.Fatalf("缺 notify.dispatch_panic 告警：%v", sink.records(t))
	}
}
