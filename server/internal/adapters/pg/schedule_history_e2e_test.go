package pg_test

// 放弃行现场的历史查询 e2e：口径全在 SQL（按账号、按天去重、today 之前），
// 用替身测不出来。判据：
//   - lastSuccess 只取 today 之前最近一次 success；
//   - failedDays 只数那次成功之后的失败天，且同一天多任务只算一天；
//   - 从未成功时 lastSuccess 为空串，失败天照数。

import (
	"fmt"
	"testing"
	"time"

	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
)

// seedHistory 造一条计划行序列并把指定日期改成给定状态。
func seedHistory(t *testing.T, e *testEnv, accountID, taskID string, states map[string]corestatus.TaskState) {
	t.Helper()
	var rows []coreschedule.PlanRow
	for day := range states {
		at, err := time.Parse("2006-01-02", day)
		if err != nil {
			t.Fatalf("bad day %q: %v", day, err)
		}
		rows = append(rows, coreschedule.PlanRow{
			AccountID: accountID, SchoolTaskID: taskID, Day: day,
			RunAt: at.Add(6*time.Hour + 40*time.Minute),
		})
	}
	if _, err := e.sched.InsertPlan(e.ctx, rows); err != nil {
		t.Fatalf("insert plan: %v", err)
	}
	for day, state := range states {
		if _, err := e.db.Exec(e.ctx, `
			UPDATE sign_tasks SET state = $4::sign_task_state, result = $5
			 WHERE account_id = $1::uuid AND school_task_id = $2::uuid AND day = $3`,
			accountID, taskID, day, string(state), string(state)); err != nil {
			t.Fatalf("seed %s = %s: %v", day, state, err)
		}
	}
}

func TestAccountSignHistoryCountsOnlyFailuresAfterLastSuccess(t *testing.T) {
	e := newEnv(t)
	_, taskID, accountID := e.seedTask(
		"history", "晨间签到", "history-a@example.com", "13800033333")
	seedHistory(t, e, accountID, taskID, map[string]corestatus.TaskState{
		"2026-10-20": corestatus.TaskSuccess,
		"2026-10-21": corestatus.TaskFailed,
		"2026-10-22": corestatus.TaskSuccess, // 最近一次成功
		"2026-10-23": corestatus.TaskFailed,
		"2026-10-24": corestatus.TaskFailed,
		"2026-10-26": corestatus.TaskFailed,
		"2026-10-27": corestatus.TaskFailed, // today 本身不计入历史
	})

	last, failedDays, err := e.sched.AccountSignHistory(e.ctx, accountID, "2026-10-27")
	if err != nil {
		t.Fatalf("AccountSignHistory: %v", err)
	}
	if last != "2026-10-22" {
		t.Fatalf("lastSuccess = %q, want 2026-10-22", last)
	}
	if failedDays != 3 {
		t.Fatalf("failedDays = %d, want 3（23/24/26；21 在成功之前，27 是 today）", failedDays)
	}
}

// 从未成功过：lastSuccess 空串，失败天照数（today 之前）。
func TestAccountSignHistoryNeverSucceeded(t *testing.T) {
	e := newEnv(t)
	_, taskID, accountID := e.seedTask(
		"history-never", "晨间签到", "history-b@example.com", "13800044444")
	seedHistory(t, e, accountID, taskID, map[string]corestatus.TaskState{
		"2026-10-24": corestatus.TaskFailed,
		"2026-10-25": corestatus.TaskSkipped, // 跳过不算失败
		"2026-10-26": corestatus.TaskFailed,
	})

	last, failedDays, err := e.sched.AccountSignHistory(e.ctx, accountID, "2026-10-27")
	if err != nil {
		t.Fatalf("AccountSignHistory: %v", err)
	}
	if last != "" {
		t.Fatalf("lastSuccess = %q, want empty", last)
	}
	if failedDays != 2 {
		t.Fatalf("failedDays = %d, want 2（skipped 不计）", failedDays)
	}
}

// 一天多任务只算一天：同账号同一 day 的两行 failed 不得计成两天。
func TestAccountSignHistoryDedupesDayAcrossTasks(t *testing.T) {
	e := newEnv(t)
	schoolID, taskA, accountID := e.seedTask(
		"history-dup", "晨间签到", "history-c@example.com", "13800055555")
	taskB, err := e.sched.CreateSchoolTask(e.ctx, schoolID, "晚间签到")
	if err != nil {
		t.Fatalf("create second task: %v", err)
	}
	day := "2026-10-26"
	for _, taskID := range []string{taskA, taskB} {
		seedHistory(t, e, accountID, taskID, map[string]corestatus.TaskState{
			day: corestatus.TaskFailed,
		})
	}
	_, failedDays, err := e.sched.AccountSignHistory(e.ctx, accountID, "2026-10-27")
	if err != nil {
		t.Fatalf("AccountSignHistory: %v", err)
	}
	if failedDays != 1 {
		t.Fatalf("failedDays = %d, want 1（同一天两个任务只算一天）: %s", failedDays, fmt.Sprint(day))
	}
}
