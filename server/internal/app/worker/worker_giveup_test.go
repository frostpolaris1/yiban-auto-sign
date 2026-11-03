package worker_test

// 放弃行自解释：一次真实失败要能在一屏内回答
// 「属哪一类 / 我方解密是否正常 / 是不是今天才坏的 / 接下来能做什么」，
// 不必再去翻备份和审计链（2026-11-03 排查凭据失效的取证成本）。
//
// 形态：放弃行与逐次尝试行同一个 msg，靠 stage=giveup 区分；
// 同一事实只出现在字段里，消息文本不复述。分类只认 contract.Outcome，
// 建议只认 core/diagnostic 的封闭词表。现场只给认证类，其余类带全场只会刷屏。

import (
	"context"
	"errors"
	"log/slog"
	"testing"
	"time"

	appworker "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/worker"
	corediagnostic "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/diagnostic"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

const testKID = "ab12cd34ef56ab78"

// newSinkLogger 把 JSON 日志写进 logSink，供逐字段断言。
func newSinkLogger(sink *logSink) *slog.Logger {
	return slog.New(slog.NewJSONHandler(sink, nil))
}

// giveUpRecords 只取放弃行：stage=giveup 是这类行唯一标识。
func giveUpRecords(records []map[string]any, outcome string) []map[string]any {
	var out []map[string]any
	for _, rec := range records {
		if rec["outcome"] == outcome && rec["stage"] == "giveup" {
			out = append(out, rec)
		}
	}
	return out
}

// advancingClock 每次读取推进 330ms。
// 放弃行的耗时取注入时钟，固定时钟恒得 0，测不出"耗时有值"这条；
// 推进幅度远小于窗口宽度，窗口判定不受影响。
type advancingClock struct {
	base  time.Time
	reads int
}

func (c *advancingClock) Now() time.Time {
	c.reads++
	return c.base.Add(time.Duration(c.reads-1) * 330 * time.Millisecond)
}

func newGiveUpService(t *testing.T, st *fakeStore, signer appworker.Signer, sink *logSink) *appworker.Service {
	t.Helper()
	svc, err := appworker.NewService(appworker.Deps{
		Store: st, Creds: fakeCreds{kid: testKID}, Signer: signer, Egress: fakeEgress{},
		Limiter: fakeLimiter{allow: true}, Clock: &advancingClock{base: testNow()}, Location: loc,
		Logger: newSinkLogger(sink),
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

// 认证失败放弃：字段要能回答类、解密是否正常、从哪天起坏、能做什么。
func TestAuthGiveUpLineIsSelfExplaining(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	st.history["acct-1"] = signHistory{lastSuccess: "2026-10-31", failedDays: 1}
	sink := &logSink{}
	signer := &scriptedSigner{
		targets: []contract.Target{{ID: "pos-1"}},
		signErr: &contract.OutcomeError{
			Outcome: contract.OutcomeCredentialBad, Message: "登录失败: 账号或密码错误"},
	}
	svc := newGiveUpService(t, st, signer, sink)

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if got := st.finished["t1"]; got != corestatus.TaskFailed {
		t.Fatalf("state = %q, want failed", got)
	}
	rows := giveUpRecords(sink.records(t), string(contract.OutcomeCredentialBad))
	if len(rows) != 1 {
		t.Fatalf("放弃行条数 = %d, want 1（记录 %v）", len(rows), sink.records(t))
	}
	rec := rows[0]
	if rec["last_success_day"] != "2026-10-31" {
		t.Fatalf("last_success = %v, want 2026-10-31", rec["last_success_day"])
	}
	// 失败天数含本次：历史 1 天 + 今天这次。
	if got, _ := rec["failed_days"].(float64); got != 2 {
		t.Fatalf("fail_days = %v, want 2", rec["failed_days"])
	}
	if rec["credential_kid"] != testKID {
		t.Fatalf("kid = %v, want %s", rec["credential_kid"], testKID)
	}
	// 一次尝试 330ms：字段用毫秒整数，与入口中间件的命名口径一致。
	if got, _ := rec["duration_ms"].(float64); got != 330 {
		t.Fatalf("duration_ms = %v, want 330", rec["duration_ms"])
	}
	// 建议语复用诊断词表：凭据错只对应 contact_admin。
	if rec["suggestion"] != corediagnostic.SuggestContactAdmin {
		t.Fatalf("suggestion = %v, want %s", rec["suggestion"], corediagnostic.SuggestContactAdmin)
	}
	// 同一事实只在字段里出现一次：消息文本不拼句子。
	if rec["msg"] != "signing outcome" {
		t.Fatalf("msg = %v, want signing outcome（不复述字段）", rec["msg"])
	}
}

// 非认证类放弃只带结论与原因：不塞指纹、历史与耗时，避免刷屏。
func TestNonAuthGiveUpCarriesNoScene(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	st.history["acct-1"] = signHistory{lastSuccess: "2026-10-20", failedDays: 0}
	sink := &logSink{}
	signer := &scriptedSigner{
		targets:    []contract.Target{{ID: "pos-1"}},
		signResult: contract.Result{Outcome: contract.OutcomeBlocked, Message: "风控拦截"},
	}
	svc := newGiveUpService(t, st, signer, sink)

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	rows := giveUpRecords(sink.records(t), string(contract.OutcomeBlocked))
	if len(rows) != 1 {
		t.Fatalf("放弃行条数 = %d, want 1（记录 %v）", len(rows), sink.records(t))
	}
	rec := rows[0]
	for _, key := range []string{"credential_kid", "duration_ms", "last_success_day", "failed_days", "history_available"} {
		if _, ok := rec[key]; ok {
			t.Fatalf("非认证类不该带 %q: %v", key, rec)
		}
	}
	// 建议词仍是既有词表里的值：窗口开着 → 可手动再签。
	if rec["suggestion"] != corediagnostic.SuggestRetryNow {
		t.Fatalf("suggestion = %v, want %s", rec["suggestion"], corediagnostic.SuggestRetryNow)
	}
}

// 词表外/unknown 也要有放弃行，且不缺建议词。
func TestUnknownOutcomeStillLogsGiveUp(t *testing.T) {
	task := baseTask()
	task.Attempts = 2 // 预算 3 次，第 3 次即放弃
	st := newStore(task)
	st.windows["st-1|2026-10-29"] = openWindow()
	sink := &logSink{}
	signer := &scriptedSigner{
		targets:    []contract.Target{{ID: "pos-1"}},
		signResult: contract.Result{Outcome: contract.OutcomeUnknown, Message: "platform state 7"},
	}
	svc := newGiveUpService(t, st, signer, sink)

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	rows := giveUpRecords(sink.records(t), string(contract.OutcomeUnknown))
	if len(rows) != 1 {
		t.Fatalf("放弃行条数 = %d, want 1（记录 %v）", len(rows), sink.records(t))
	}
	if rows[0]["suggestion"] == nil {
		t.Fatalf("放弃行缺建议词: %v", rows[0])
	}
}

// 历史查询失败不得改变签到结论，也不得让日志缺失。
func TestHistoryQueryFailureKeepsSigningOutcome(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	st.historyErr = errors.New("db down")
	sink := &logSink{}
	signer := &scriptedSigner{
		targets: []contract.Target{{ID: "pos-1"}},
		signErr: &contract.OutcomeError{
			Outcome: contract.OutcomeCredentialBad, Message: "登录失败: 账号或密码错误"},
	}
	svc := newGiveUpService(t, st, signer, sink)

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if got := st.finished["t1"]; got != corestatus.TaskFailed {
		t.Fatalf("state = %q, want failed", got)
	}
	rows := giveUpRecords(sink.records(t), string(contract.OutcomeCredentialBad))
	if len(rows) != 1 {
		t.Fatalf("放弃行条数 = %d, want 1", len(rows))
	}
	if _, ok := rows[0]["last_success_day"]; ok {
		t.Fatalf("历史查询失败时不该给 last_success: %v", rows[0])
	}
	if rows[0]["history_available"] != false {
		t.Fatalf("history_available = %v, want false（缺口要显性）", rows[0]["history_available"])
	}
}

// 从未成功过的账号：仍要能出放弃行，且明确写 never 而不是留空。
func TestGiveUpLineForNeverSucceededAccount(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	st.history["acct-1"] = signHistory{lastSuccess: "", failedDays: 0}
	sink := &logSink{}
	signer := &scriptedSigner{
		targets: []contract.Target{{ID: "pos-1"}},
		signErr: &contract.OutcomeError{
			Outcome: contract.OutcomeCredentialBad, Message: "登录失败: 账号或密码错误"},
	}
	svc := newGiveUpService(t, st, signer, sink)

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	rows := giveUpRecords(sink.records(t), string(contract.OutcomeCredentialBad))
	if len(rows) != 1 {
		t.Fatalf("放弃行条数 = %d, want 1", len(rows))
	}
	if rows[0]["last_success_day"] != "never" {
		t.Fatalf("last_success = %v, want never", rows[0]["last_success_day"])
	}
}

// 逐次尝试行不带现场：同一份现场只应出现在当日的放弃行里。
func TestAttemptLineCarriesNoScene(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	sink := &logSink{}
	signer := &scriptedSigner{
		targets:    []contract.Target{{ID: "pos-1"}},
		signResult: contract.Result{Outcome: contract.OutcomeTransient, Message: "timeout"},
	}
	svc := newGiveUpService(t, st, signer, sink)

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	records := sink.records(t)
	found := false
	for _, rec := range records {
		if rec["outcome"] != string(contract.OutcomeTransient) || rec["stage"] == "giveup" {
			continue
		}
		found = true
		for _, key := range []string{"credential_kid", "suggestion", "duration_ms", "last_success_day", "history_available"} {
			if _, ok := rec[key]; ok {
				t.Fatalf("尝试行不该带 %q: %v", key, rec)
			}
		}
	}
	if !found {
		t.Fatalf("未记录 transient 尝试行: %v", records)
	}
}
