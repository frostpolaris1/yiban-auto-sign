package worker_test

// 手动签到的「同账号同日只认一次成功」：Request 在解析任务之前先问
// HasSuccessOn，true 直接 already_signed——不为兄弟任务建行。
//
// 判据钉在调用点：成功已被拦下时，DayTasks / AccountEnabledTasks /
// EnsureTask 都不能被调用；查询故障必须上抛，不得读成「未成功」。
// 只认 state='success'：skipped 不拦（可重开），跨日 success 不拦。

import (
	"context"
	"errors"
	"testing"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appworker "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/worker"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
)

// TestRequestSiblingTaskSuccessReturnsAlreadySigned 钉住决策 2A 的核心形状：
// 账号当日 A 已 success，手动签到必须回 already_signed，且**不再为 B 建行**。
//
// 造数：学校有 A/B 两个启用任务、窗口都开着；只有 A 的 success 行。
// 若 Request 先 resolveTask 再判行状态，会走到 EnsureTask：picker 选中 B 时
// 建出 B 行（本用例红）；即使随机选中 A，dayTasksCalls 也暴露了顺序错误。
func TestRequestSiblingTaskSuccessReturnsAlreadySigned(t *testing.T) {
	ctx := context.Background()
	actor := appaudit.Actor{UserID: "u1"}
	st := newStore()
	st.enabledTasks = []string{"st-a", "st-b"}
	st.windows["st-a|2026-10-29"] = openWindow()
	st.windows["st-b|2026-10-29"] = openWindow()
	st.tasks = []coreschedule.TaskRow{{
		ID: "ta", AccountID: "acct-1", SchoolTaskID: "st-a",
		Day: "2026-10-29", State: string(corestatus.TaskSuccess),
	}}
	picked := false
	svc := newServiceWithPicker(t, st, defaultSigner(), fakeLimiter{allow: true},
		func(c []string) string { picked = true; return c[len(c)-1] })

	if _, err := svc.Request(ctx, "acct-1", actor); !errors.Is(err, appworker.ErrAlreadySigned) {
		t.Fatalf("Request err = %v, want ErrAlreadySigned", err)
	}
	if picked {
		t.Fatal("已成功账号不得进入任务解析/选点")
	}
	if st.dayTasksCalls != 0 || st.enabledTaskCalls != 0 {
		t.Fatalf("resolveTask calls = day:%d enabled:%d, want 0/0",
			st.dayTasksCalls, st.enabledTaskCalls)
	}
	if st.ensureCalls != 0 {
		t.Fatalf("EnsureTask calls = %d, want 0（不得为兄弟任务建行）", st.ensureCalls)
	}
	for _, row := range st.tasks {
		if row.SchoolTaskID == "st-b" {
			t.Fatalf("已成功账号仍为兄弟任务建了行: %+v", row)
		}
	}
	if got := st.hasSuccessCalls; len(got) != 1 || got[0] != "acct-1|2026-10-29" {
		t.Fatalf("HasSuccessOn calls = %v, want [acct-1|2026-10-29]", got)
	}
}

// TestRequestUsesHasSuccessOnBeforeResolve 钉住前置检查本身：
// 即使当日没有任何任务行，HasSuccessOn=true 也必须直接 already_signed。
//
// 删掉 Request 的前置检查时，本用例会为唯一启用任务建行并返回行 ID——红。
func TestRequestUsesHasSuccessOnBeforeResolve(t *testing.T) {
	ctx := context.Background()
	actor := appaudit.Actor{UserID: "u1"}
	st := newStore()
	st.hasSuccess = true
	st.enabledTasks = []string{"st-1"}
	st.windows["st-1|2026-10-29"] = openWindow()
	svc := newService(t, st, defaultSigner(), fakeLimiter{allow: true})

	if _, err := svc.Request(ctx, "acct-1", actor); !errors.Is(err, appworker.ErrAlreadySigned) {
		t.Fatalf("Request err = %v, want ErrAlreadySigned", err)
	}
	if st.dayTasksCalls != 0 || st.enabledTaskCalls != 0 || st.ensureCalls != 0 {
		t.Fatalf("前置检查后仍解析/建行: day:%d enabled:%d ensure:%d",
			st.dayTasksCalls, st.enabledTaskCalls, st.ensureCalls)
	}
	if len(st.tasks) != 0 {
		t.Fatalf("已成功账号建了 %d 行, want 0", len(st.tasks))
	}
}

// TestRequestHasSuccessOnErrorPropagates 钉住失败关闭：
// 查询 success 失败必须原样上抛，不得读成「未成功」继续入队。
//
// 把 error 当成 false 时，本用例会成功建行（红）。
func TestRequestHasSuccessOnErrorPropagates(t *testing.T) {
	ctx := context.Background()
	actor := appaudit.Actor{UserID: "u1"}
	sentinel := errors.New("schedule store unavailable")
	st := newStore()
	st.hasSuccessErr = sentinel
	st.enabledTasks = []string{"st-1"}
	st.windows["st-1|2026-10-29"] = openWindow()
	svc := newService(t, st, defaultSigner(), fakeLimiter{allow: true})

	_, err := svc.Request(ctx, "acct-1", actor)
	if !errors.Is(err, sentinel) {
		t.Fatalf("Request err = %v, want the store error", err)
	}
	if errors.Is(err, appworker.ErrAlreadySigned) {
		t.Fatal("查询故障不得归一成 already_signed")
	}
	if st.ensureCalls != 0 || len(st.tasks) != 0 {
		t.Fatalf("查询故障仍入队: ensure=%d rows=%d", st.ensureCalls, len(st.tasks))
	}
}

// TestRequestSuccessOnAnotherDayDoesNotBlock 钉住按业务日判定：
// 昨天 A success 不影响今天手动签到。
//
// 把 day 条件删掉（只按账号查 success）时本用例红。
func TestRequestSuccessOnAnotherDayDoesNotBlock(t *testing.T) {
	ctx := context.Background()
	actor := appaudit.Actor{UserID: "u1"}
	st := newStore()
	st.enabledTasks = []string{"st-1"}
	st.windows["st-1|2026-10-29"] = openWindow()
	st.tasks = []coreschedule.TaskRow{{
		ID: "ty", AccountID: "acct-1", SchoolTaskID: "st-1",
		Day: "2026-10-28", State: string(corestatus.TaskSuccess),
	}}
	svc := newService(t, st, defaultSigner(), fakeLimiter{allow: true})

	id, err := svc.Request(ctx, "acct-1", actor)
	if err != nil || id == "" {
		t.Fatalf("Request = (%q,%v), want a queued row", id, err)
	}
}
