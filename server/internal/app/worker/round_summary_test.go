package worker_test

// 轮末汇总（round summary）：
// 每个业务日结束时把上一日的 sign_tasks 结果记成一行结构化日志。
// 判据是日志字段面：day/total/success/failed/skipped，失败日带
// outcome_counts，total=0 不出行，查询失败只告警不影响 Step。

import (
	"context"
	"errors"
	"log/slog"
	"testing"

	appworker "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/worker"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/logfield"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
)

// newSummaryService 用可注入的时钟与日志 sink 构造服务。
func newSummaryService(t *testing.T, st *fakeStore, clk coreclock.Clock, logger *slog.Logger) *appworker.Service {
	t.Helper()
	svc, err := appworker.NewService(appworker.Deps{
		Store: st, Creds: fakeCreds{}, Signer: defaultSigner(), Egress: fakeEgress{},
		Limiter: fakeLimiter{allow: true}, Clock: clk, Location: loc, Logger: logger,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

// recordsWithMsg 返回指定消息的全部日志记录。
func recordsWithMsg(recs []map[string]any, msg string) []map[string]any {
	var out []map[string]any
	for _, rec := range recs {
		if rec["msg"] == msg {
			out = append(out, rec)
		}
	}
	return out
}

// wantCount 断言日志里的计数键（JSON 解出的是 float64）。
func wantCount(t *testing.T, rec map[string]any, key string, want int) {
	t.Helper()
	got, ok := rec[key].(float64)
	if !ok {
		t.Fatalf("%s = %v（%T），want %d", key, rec[key], rec[key], want)
	}
	if int(got) != want {
		t.Fatalf("%s = %v, want %d", key, rec[key], want)
	}
}

// outcomeCount 取 outcome_counts 里某个词的数量；缺失返回 -1。
func outcomeCount(rec map[string]any, outcome string) int {
	raw, ok := rec[logfield.KeyOutcomeCounts].(map[string]any)
	if !ok {
		return -1
	}
	n, ok := raw[outcome].(float64)
	if !ok {
		return -1
	}
	return int(n)
}

// 首跑：汇总前一日，字段按任务行口径。
func TestRoundSummaryFirstRunReportsPreviousDay(t *testing.T) {
	st := newStore()
	st.dayCounts["2026-10-28"] = appworker.DayOutcomeCounts{
		Total: 5, Success: 3, Failed: 2, Skipped: 0,
		FailedByOutcome: map[string]int{"rejected": 1, "blocked": 1},
	}
	sink := &logSink{}
	svc := newSummaryService(t, st, coreclock.Fixed{T: testNow()}, newSinkLogger(sink))

	if n, err := svc.Step(context.Background(), 1); err != nil || n != 0 {
		t.Fatalf("Step = (%d,%v), want (0,nil)", n, err)
	}
	recs := recordsWithMsg(sink.records(t), "round summary")
	if len(recs) != 1 {
		t.Fatalf("汇总行数 = %d, want 1", len(recs))
	}
	rec := recs[0]
	if rec[logfield.KeyDay] != "2026-10-28" {
		t.Fatalf("day = %v, want 2026-10-28", rec[logfield.KeyDay])
	}
	wantCount(t, rec, logfield.KeyTotal, 5)
	wantCount(t, rec, logfield.KeySuccess, 3)
	wantCount(t, rec, logfield.KeyFailed, 2)
	wantCount(t, rec, logfield.KeySkipped, 0)
	if _, ok := rec[logfield.KeyPending]; ok {
		t.Fatalf("pending=0 不该带 pending 键: %v", rec[logfield.KeyPending])
	}
	if got := outcomeCount(rec, "rejected"); got != 1 {
		t.Fatalf("outcome_counts[rejected] = %d, want 1", got)
	}
	if got := outcomeCount(rec, "blocked"); got != 1 {
		t.Fatalf("outcome_counts[blocked] = %d, want 1", got)
	}
	if rec["level"] != "WARN" {
		t.Fatalf("有失败时级别 = %v, want WARN", rec["level"])
	}
}

// 零失败：出汇总行，但不带 outcome_counts，级别 Info。
func TestRoundSummaryNoFailuresOmitsOutcomeCounts(t *testing.T) {
	st := newStore()
	st.dayCounts["2026-10-28"] = appworker.DayOutcomeCounts{
		Total: 4, Success: 3, Skipped: 1,
	}
	sink := &logSink{}
	svc := newSummaryService(t, st, coreclock.Fixed{T: testNow()}, newSinkLogger(sink))

	if _, err := svc.Step(context.Background(), 1); err != nil {
		t.Fatalf("Step: %v", err)
	}
	recs := recordsWithMsg(sink.records(t), "round summary")
	if len(recs) != 1 {
		t.Fatalf("汇总行数 = %d, want 1", len(recs))
	}
	rec := recs[0]
	if _, ok := rec[logfield.KeyOutcomeCounts]; ok {
		t.Fatalf("failed=0 不该带 outcome_counts: %v", rec[logfield.KeyOutcomeCounts])
	}
	wantCount(t, rec, logfield.KeyTotal, 4)
	wantCount(t, rec, logfield.KeySuccess, 3)
	wantCount(t, rec, logfield.KeyFailed, 0)
	wantCount(t, rec, logfield.KeySkipped, 1)
	if rec["level"] != "INFO" {
		t.Fatalf("无失败时级别 = %v, want INFO", rec["level"])
	}
}

// 空 result 的失败行归 unknown：与字面 unknown 合并计数。
func TestRoundSummaryEmptyResultBecomesUnknown(t *testing.T) {
	st := newStore()
	st.dayCounts["2026-10-28"] = appworker.DayOutcomeCounts{
		Total: 2, Failed: 2,
		FailedByOutcome: map[string]int{"": 1, "unknown": 1},
	}
	sink := &logSink{}
	svc := newSummaryService(t, st, coreclock.Fixed{T: testNow()}, newSinkLogger(sink))

	if _, err := svc.Step(context.Background(), 1); err != nil {
		t.Fatalf("Step: %v", err)
	}
	recs := recordsWithMsg(sink.records(t), "round summary")
	if len(recs) != 1 {
		t.Fatalf("汇总行数 = %d, want 1", len(recs))
	}
	if got := outcomeCount(recs[0], "unknown"); got != 2 {
		t.Fatalf("outcome_counts[unknown] = %d, want 2（空串与字面 unknown 合并）", got)
	}
	if raw, _ := recs[0]["outcome_counts"].(map[string]any); len(raw) != 1 {
		t.Fatalf("outcome_counts 应只有 unknown 一个桶: %v", recs[0]["outcome_counts"])
	}
	if got := outcomeCount(recs[0], ""); got != -1 {
		t.Fatalf("outcome_counts 不该保留空键: %d", got)
	}
}

// 查询失败：只出一条 Warn，Step 与签到结论照常。
func TestRoundSummaryQueryFailureDoesNotAffectStep(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	st.dayCountsErr = errors.New("db down")
	sink := &logSink{}
	svc := newSummaryService(t, st, coreclock.Fixed{T: testNow()}, newSinkLogger(sink))

	n, err := svc.Step(context.Background(), 10)
	if err != nil || n != 1 {
		t.Fatalf("Step = (%d,%v), want (1,nil)", n, err)
	}
	if st.finished["t1"] != corestatus.TaskSuccess {
		t.Fatalf("签到结论 = %v, want success", st.finished["t1"])
	}
	recs := sink.records(t)
	failed := recordsWithMsg(recs, "round summary failed")
	if len(failed) != 1 {
		t.Fatalf("失败告警行数 = %d, want 1", len(failed))
	}
	if failed[0]["day"] != "2026-10-28" {
		t.Fatalf("告警 day = %v, want 2026-10-28", failed[0]["day"])
	}
	if msg, _ := failed[0]["error"].(string); msg == "" {
		t.Fatal("告警必须带 error")
	}
	if failed[0]["level"] != "WARN" {
		t.Fatalf("告警级别 = %v, want WARN", failed[0]["level"])
	}
	if got := recordsWithMsg(recs, "round summary"); len(got) != 0 {
		t.Fatalf("查询失败不得再出汇总行: %d", len(got))
	}
}

// 跨日：业务日变化时只汇总上一次见过的业务日，同一日不重复。
func TestRoundSummaryOncePerBusinessDay(t *testing.T) {
	st := newStore()
	st.dayCounts["2026-10-28"] = appworker.DayOutcomeCounts{Total: 1, Success: 1}
	st.dayCounts["2026-10-29"] = appworker.DayOutcomeCounts{
		Total: 2, Success: 1, Failed: 1,
		FailedByOutcome: map[string]int{"": 1},
	}
	clk := &stepClock{t: testNow()}
	sink := &logSink{}
	svc := newSummaryService(t, st, clk, newSinkLogger(sink))

	ctx := context.Background()
	for i := 0; i < 2; i++ {
		if _, err := svc.Step(ctx, 1); err != nil {
			t.Fatalf("同日 Step %d: %v", i, err)
		}
	}
	clk.t = clk.t.AddDate(0, 0, 1)
	for i := 0; i < 2; i++ {
		if _, err := svc.Step(ctx, 1); err != nil {
			t.Fatalf("跨日 Step %d: %v", i, err)
		}
	}

	if len(st.dayCountCalls) != 2 ||
		st.dayCountCalls[0] != "2026-10-28" || st.dayCountCalls[1] != "2026-10-29" {
		t.Fatalf("查询日次 = %v, want [2026-10-28 2026-10-29]", st.dayCountCalls)
	}
	recs := recordsWithMsg(sink.records(t), "round summary")
	if len(recs) != 2 {
		t.Fatalf("汇总行数 = %d, want 2", len(recs))
	}
	if recs[0]["day"] != "2026-10-28" || recs[1]["day"] != "2026-10-29" {
		t.Fatalf("汇总日 = %v/%v, want 10-28/10-29", recs[0]["day"], recs[1]["day"])
	}
}

// total=0：不出汇总行。
func TestRoundSummarySkipsEmptyDay(t *testing.T) {
	st := newStore()
	sink := &logSink{}
	svc := newSummaryService(t, st, coreclock.Fixed{T: testNow()}, newSinkLogger(sink))

	if _, err := svc.Step(context.Background(), 1); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if len(st.dayCountCalls) != 1 || st.dayCountCalls[0] != "2026-10-28" {
		t.Fatalf("查询日次 = %v, want [2026-10-28]", st.dayCountCalls)
	}
	if recs := recordsWithMsg(sink.records(t), "round summary"); len(recs) != 0 {
		t.Fatalf("total=0 不得出汇总行: %v", recs)
	}
}
