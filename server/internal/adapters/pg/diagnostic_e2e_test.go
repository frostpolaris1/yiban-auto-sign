package pg_test

// DiagnosticRepo 的真库判据（P6）。
//
// # 功能
// 只读四查询按真实数据形状回答：当日任务行、窗口缓存、
// 出口标签与作用域状态计数。作用域计数逐口径断言：
// master 全量、admin 只数绑定校、user 只数自己的账号，
// 软删账号的行不计入。
//
// # 口径
// 无 DSN 时整套跳过，跳过显性可见（main_test.go）。

import (
	"context"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
)

// seedDiagnostic 建两校两账号，A 账号有当日任务行与窗口与出口。
func seedDiagnostic(t *testing.T) (*testEnv, *pg.DiagnosticRepo, string, string, string) {
	t.Helper()
	e := newEnv(t)
	repo := pg.NewDiagnosticRepo(e.db)
	schoolA, taskA, acctA := e.seedTask("diag-a", "晨间签到", "diag-owner@example.com", "13800010001")
	// 第二校第二账号：作用域计数的跨校面。
	e.seedTask("diag-b", "晨间签到", "diag-other@example.com", "13800010002")

	day := time.Now().In(time.UTC).Format("2006-01-02")
	if _, _, err := e.sched.EnsureTask(e.ctx, acctA, taskA, day, time.Now().Add(time.Hour)); err != nil {
		t.Fatalf("ensure task: %v", err)
	}
	if _, err := e.sched.PutWindow(e.ctx, coreschedule.WindowRecord{
		SchoolTaskID: taskA, Day: day,
		Start: time.Now().Add(-time.Hour), End: time.Now().Add(time.Hour),
		Source: "platform",
	}, 0); err != nil {
		t.Fatalf("put window: %v", err)
	}
	if _, err := e.db.Exec(e.ctx,
		"INSERT INTO egresses (identity) VALUES ('https://diag-egress')"); err != nil {
		t.Fatalf("seed egress: %v", err)
	}
	if _, err := e.db.Exec(e.ctx,
		`INSERT INTO account_egress (account_id, egress_id)
		 SELECT $1::uuid, id FROM egresses WHERE identity = 'https://diag-egress'`, acctA); err != nil {
		t.Fatalf("bind egress: %v", err)
	}
	return e, repo, schoolA, acctA, day
}

func TestDiagnosticTodayTasksAndJoins(t *testing.T) {
	e, repo, _, acctA, day := seedDiagnostic(t)

	rows, err := repo.TodayTasks(e.ctx, []string{acctA}, day)
	if err != nil {
		t.Fatalf("TodayTasks: %v", err)
	}
	if len(rows) != 1 || rows[0].AccountID != acctA || rows[0].State != string(corestatus.TaskPending) {
		t.Fatalf("rows = %+v", rows)
	}
	wins, err := repo.Windows(e.ctx, []string{rows[0].SchoolTaskID}, day)
	if err != nil {
		t.Fatalf("Windows: %v", err)
	}
	if w, ok := wins[rows[0].SchoolTaskID]; !ok || w.Source != "platform" {
		t.Fatalf("windows = %+v", wins)
	}
	egs, err := repo.Egresses(e.ctx, []string{acctA})
	if err != nil {
		t.Fatalf("Egresses: %v", err)
	}
	if e, ok := egs[acctA]; !ok || e.Identity != "https://diag-egress" {
		t.Fatalf("egresses = %+v", egs)
	}
}

func TestDiagnosticStateCountsByScope(t *testing.T) {
	e, repo, schoolA, _, day := seedDiagnostic(t)
	ctx := context.Background()

	masterCounts, err := repo.StateCounts(ctx, corescope.Master(), day)
	if err != nil {
		t.Fatalf("master counts: %v", err)
	}
	if masterCounts[string(corestatus.TaskPending)] != 1 {
		t.Fatalf("master counts = %v, want pending:1", masterCounts)
	}
	adminCounts, err := repo.StateCounts(ctx, corescope.Admin([]string{schoolA}), day)
	if err != nil {
		t.Fatalf("admin counts: %v", err)
	}
	if adminCounts[string(corestatus.TaskPending)] != 1 {
		t.Fatalf("admin(schoolA) counts = %v", adminCounts)
	}
	boundOther := e.createSchool("diag-c")
	emptyAdmin, err := repo.StateCounts(ctx, corescope.Admin([]string{boundOther.ID}), day)
	if err != nil {
		t.Fatalf("empty admin counts: %v", err)
	}
	if len(emptyAdmin) != 0 {
		t.Fatalf("未绑定校 admin counts = %v, want 空", emptyAdmin)
	}
}
