package worker

// 轮末汇总：每个业务日结束时把上一日的任务结果记成一行结构化日志。
//
// # 功能
// 进程首次跑 Step 时汇总前一日；之后业务日每变化一次，汇总上一次见过的
// 业务日。同一进程同一日只汇总一次（内存状态）；进程重启会重复汇总
// 上一日，属至少一次语义，不追求幂等。
// 本文件**不**写库、**不**改任务状态、**不**参与签到判定。
//
// # 归属
// app/worker 的诊断面附件。判据在 Service.Step 开头触发。
//
// # 口径
// 「轮」= 业务日：用 deps.Location（上海）从注入时钟取 YYYY-MM-DD。
// 汇总按 sign_tasks 任务行计数，不做账号去重；Pending 含 pending 与 running。
// outcome_counts 只统计 failed 行，空 result 归 unknown。
// 前一日 total=0 不出汇总行；failed=0 出汇总行但不带 outcome_counts。
// 查询失败只记一条 Warn，不影响 Step 与签到结论；失败当日不再重试，
// 避免每 tick 刷同一条告警。

import (
	"context"
	"log/slog"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/logfield"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// dayLayout 是业务日串的格式，与日志字段 day 的语义一致。
const dayLayout = "2006-01-02"

// summarizePreviousDay 汇总上一次见过的业务日。
//
// 状态机（lastDay 只由本函数读写）：
//   - 首跑（lastDay 为空）：汇总 today-1；
//   - 跨日：汇总 lastDay，再把 lastDay 推到 today；
//   - 同日：直接返回。
//
// 锁只保护 lastDay：查询在锁外。Step 生产上串行，锁只负责"同一进程
// 同一业务日只汇总一次"；并发调用时后到者看到 today 即返回。
func (s *Service) summarizePreviousDay(ctx context.Context, now time.Time) {
	local := now.In(s.deps.Location)
	today := local.Format(dayLayout)

	s.summaryMu.Lock()
	if s.lastDay == today {
		s.summaryMu.Unlock()
		return
	}
	day := s.lastDay
	if day == "" {
		day = local.AddDate(0, 0, -1).Format(dayLayout)
	}
	// 先推进 lastDay：查询失败也不在同一业务日重试，
	// 否则一次库抖动会变成每 tick 一条相同告警。
	s.lastDay = today
	s.summaryMu.Unlock()

	counts, err := s.deps.Store.DayOutcomeCounts(ctx, day)
	if err != nil {
		s.log.Warn("round summary failed", logfield.KeyDay, day, logfield.KeyError, err.Error())
		return
	}
	if counts.Total == 0 {
		return
	}

	level := slog.LevelInfo
	attrs := []any{
		logfield.KeyDay, day,
		logfield.KeyTotal, counts.Total,
		logfield.KeySuccess, counts.Success,
		logfield.KeyFailed, counts.Failed,
		logfield.KeySkipped, counts.Skipped,
	}
	if counts.Pending > 0 {
		attrs = append(attrs, logfield.KeyPending, counts.Pending)
	}
	if counts.Failed > 0 {
		attrs = append(attrs, logfield.KeyOutcomeCounts, failedOutcomes(counts.FailedByOutcome))
		level = slog.LevelWarn
	}
	s.log.Log(ctx, level, "round summary", attrs...)
}

// failedOutcomes 把失败分布归一为日志字段：空 result 归 unknown，
// 与字面 unknown 合并计数。返回值非 nil，保证 JSON 里始终是对象。
func failedOutcomes(raw map[string]int) map[string]int {
	out := make(map[string]int, len(raw))
	for result, n := range raw {
		key := result
		if key == "" {
			key = string(contract.OutcomeUnknown)
		}
		out[key] += n
	}
	return out
}
