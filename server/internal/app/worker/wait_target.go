// Position 空（平台没放数据）的等待型重扫纪律。
//
// # 判据
// 发现返回空点位或 no_position 结论时不当当日结论：行退回 pending，
// 记等待词 wait_target:N（N = 已排定的重扫次数），按 base 5 分钟
// 指数退避＋抖动重扫，最多 K=3 次；落点受窗口尾约束——下一跳放不下
// 或 K 耗尽仍无点位，才落 skipped(no_target) 终态。
// State 2/3/4 的平台明确结论（no_target/already_done）不进本路径。
//
// # 口径
// 等待不计业务尝试数（RequeueWait）：重扫吃不到 transient 的业务预算，
// 预算分档仍由 core/retry.BudgetFor 唯一定义。
// 重扫落点复用 core/retry.NextDelay 的窗口判据：与业务重试同一道
// 「不越过窗口终点、尾段放不下即放弃」的纪律，不另立 margin 常量。

package worker

import (
	"context"
	"fmt"
	"strconv"
	"strings"
	"time"

	coreretry "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/retry"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	corewindow "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/window"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

const (
	// waitTarget 是 Position 空的等待词（内部尝试结果，非结论词）。
	// 冒号后缀是已排定的重扫次数：wait_target:2 = 已排 2 次重扫。
	waitTarget = "wait_target"
	// waitTargetRescans 是重扫次数上限 K：首次发现空之后最多再扫 3 次。
	waitTargetRescans = 3
	// waitTargetBase 是重扫的基础间隔：曲线由 core/retry 统一计算，
	// 5 分钟起步、按档指数放大，受 MaxBackoff 与窗口尾 cap 约束。
	waitTargetBase = 5 * time.Minute
)

// waitTargetRescanCount 从等待词解析已排定的重扫次数。
// 非等待词（结论词、空串）或后缀非法一律 0：脏数据按首次等待处理。
func waitTargetRescanCount(result string) int {
	rest, ok := strings.CutPrefix(result, waitTarget+":")
	if !ok {
		return 0
	}
	n, err := strconv.Atoi(rest)
	if err != nil || n < 0 {
		return 0
	}
	return n
}

// waitTarget 把一次「平台没放数据」转成等待型重扫。
//
// 已排满 K 次或窗口尾放不下下一跳（NextDelay 的既有判据）时落
// skipped(no_target) 终态——词与 State 2 的明确结论一致：对账面它仍是
// 「当日没有可签目标」。否则按第 N+1 档退避重排，等待词计数加一。
func (s *Service) waitTarget(ctx context.Context, task corescheduleTask, win corewindow.Window, now time.Time) error {
	n := waitTargetRescanCount(task.Result)
	if n >= waitTargetRescans {
		return s.finish(ctx, task, corestatus.TaskSkipped, string(contract.OutcomeNoTarget))
	}
	delay, ok := coreretry.NextDelay(
		coreretry.Policy{After: waitTargetBase},
		n+1, coreretry.Window{End: win.End}, now)
	if !ok {
		return s.finish(ctx, task, corestatus.TaskSkipped, string(contract.OutcomeNoTarget))
	}
	return s.requeueWait(ctx, task, now.Add(delay), fmt.Sprintf("%s:%d", waitTarget, n+1))
}
