package main

// Position 空（平台没放数据）的等待型重扫：真库、真仓储。
//
// # 功能
// 空点位行不落终态：sign_tasks 保持 pending，result 记 wait_target:1，
// 诊断读面给出 next_retry_at 与 wait 建议；窗口尾放不下下一跳时落
// skipped(no_target)；K 次重扫耗尽落同一终态；手动重开清退避立即重扫；
// State 2 的明确结论（no_target）不进等待，仍当日终态。
//
// # 口径
// 复用 loop e2e 的形状：每用例独立 schema + 迁移；时间推进用可变时钟，
// run_at 从库读回断言。平台协议不在本面：Signer 用替身。

import (
	"context"
	"fmt"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appdiagnostic "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/diagnostic"
	appegress "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/egress"
	appworker "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/worker"
	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corerate "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/rate"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	coreschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/school"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// wtClock 是可变时钟：Step 轮次之间手动推进。
type wtClock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *wtClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *wtClock) Advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = c.t.Add(d)
}

// wtSigner 是空点位替身：DiscoverTargets 返回预置点位或结论错误。
type wtSigner struct {
	mu        sync.Mutex
	targets   []contract.Target
	err       error
	outcome   contract.Result
	signCalls int
}

func (s *wtSigner) DiscoverTargets(context.Context, contract.DiscoverRequest) ([]contract.Target, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.err != nil {
		return nil, s.err
	}
	return s.targets, nil
}

func (s *wtSigner) Sign(context.Context, contract.SignRequest) (contract.Result, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.signCalls++
	return s.outcome, nil
}

// wtAccounts 满足诊断面的账号读取端口：单账号固定返回。
type wtAccounts struct{ rec appaccount.Record }

func (a wtAccounts) List(context.Context, corescope.Scope, appaccount.ListFilter, string, int) (appaccount.Page, error) {
	return appaccount.Page{Items: []appaccount.Record{a.rec}}, nil
}

func (a wtAccounts) Get(_ context.Context, _ corescope.Scope, id string) (appaccount.Record, error) {
	if id == a.rec.ID {
		return a.rec, nil
	}
	return appaccount.Record{}, coreerrs.ErrNotFound
}

// wtStack 是一个用例的库面：schema、种子数据、worker 与诊断服务。
type wtStack struct {
	t       *testing.T
	ctx     context.Context
	admin   *pg.DB
	db      *pg.DB
	schema  string
	clk     *wtClock
	sched   *pg.ScheduleRepo
	worker  *appworker.Service
	diag    *appdiagnostic.Service
	signer  *wtSigner
	acct    coreaccount.Record
	ownerID string
	taskID  string
	day     string
	start   time.Time
}

func newWtStack(t *testing.T, signer *wtSigner) *wtStack {
	t.Helper()
	base := strings.TrimSpace(os.Getenv(authDSNEnv))
	if base == "" {
		t.Skip("SKIP: " + authDSNEnv + " is not set. Wait-target e2e did NOT run.")
	}
	ctx := context.Background()
	schema := "wttest_" + randomHex(8)
	admin, err := pg.Open(ctx, pg.Options{DSN: base, MaxConns: 1, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open admin: %v", err)
	}
	if _, err := admin.Exec(ctx, "CREATE SCHEMA "+schema); err != nil {
		admin.Close()
		t.Fatalf("create schema: %v", err)
	}
	dsn := withSearchPath(base, schema)
	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 4, StatementTimeout: 10 * time.Second})
	if err != nil {
		dropWtSchema(t, admin, schema)
		admin.Close()
		t.Fatalf("open schema db: %v", err)
	}
	t.Cleanup(func() {
		db.Close()
		dropWtSchema(t, admin, schema)
		admin.Close()
	})
	if _, err := pg.Migrate(ctx, dsn); err != nil {
		t.Fatalf("migrate: %v", err)
	}

	// 业务日按 UTC 算（worker 注入 UTC 时区），种子时刻也取 UTC。
	now := coreclock.System{}.Now().UTC().Truncate(time.Second)
	clk := &wtClock{t: now}
	users := pg.NewUserRepo(db)
	var owner coreuser.Record
	if err := db.InTx(ctx, func(ctx context.Context) error {
		var txErr error
		owner, txErr = users.Create(ctx, coreuser.New{
			Email: "wt-owner@example.com", PasswordHash: "argon2id$test", Role: coreauth.RoleUser,
		}, 10000)
		return txErr
	}); err != nil {
		t.Fatalf("create user: %v", err)
	}
	var reviewer coreuser.Record
	if err := db.InTx(ctx, func(ctx context.Context) error {
		var txErr error
		reviewer, txErr = users.Create(ctx, coreuser.New{
			Email: "wt-reviewer@example.com", PasswordHash: "argon2id$test", Role: coreauth.RoleMaster,
		}, 10000)
		return txErr
	}); err != nil {
		t.Fatalf("create reviewer: %v", err)
	}
	schools := pg.NewSchoolRepo(db)
	school, err := schools.Create(ctx, coreschool.New{Code: "wtschool", Name: "WT School"})
	if err != nil {
		t.Fatalf("create school: %v", err)
	}
	accts := pg.NewAccountRepo(db)
	var acct coreaccount.Record
	if err := db.InTx(ctx, func(ctx context.Context) error {
		var txErr error
		acct, txErr = accts.Create(ctx, coreaccount.New{
			SchoolID: school.ID, OwnerUserID: owner.ID, Phone: "13800070001",
		}, 10000, 10000)
		return txErr
	}); err != nil {
		t.Fatalf("create account: %v", err)
	}
	if _, err := accts.Review(ctx, corescope.Master(), acct.ID, true, "", reviewer.ID, now); err != nil {
		t.Fatalf("activate account: %v", err)
	}
	audits, err := pg.NewAuditRepo(db, []byte("wt-e2e-audit-key-0123456789abcdef00"))
	if err != nil {
		t.Fatalf("audit repo: %v", err)
	}
	sched := pg.NewScheduleRepoWithAudit(db, audits)
	taskID, err := sched.CreateSchoolTask(ctx, school.ID, "晨签")
	if err != nil {
		t.Fatalf("create task: %v", err)
	}
	day := now.Format("2006-01-02")
	if _, err := db.Exec(ctx, "INSERT INTO egresses (identity) VALUES ($1)", "https://egress-wt-e2e"); err != nil {
		t.Fatalf("seed egress: %v", err)
	}

	egressSvc, err := appegress.NewService(appegress.Deps{
		Store: pg.NewEgressRepo(db), Clock: clk,
	})
	if err != nil {
		t.Fatalf("egress service: %v", err)
	}
	workerSvc, err := appworker.NewService(appworker.Deps{
		Store: sched, Creds: loopCreds{}, Signer: signer,
		Egress:  egressSvc,
		Limiter: corerate.NewLimiter(corerate.DefaultRate, 6),
		Clock:   clk, Location: time.UTC,
	})
	if err != nil {
		t.Fatalf("worker service: %v", err)
	}
	diagSvc, err := appdiagnostic.NewService(appdiagnostic.Deps{
		Accounts: wtAccounts{rec: appaccount.Record{
			ID: acct.ID, OwnerUserID: owner.ID, Phone: acct.Phone,
			DisplayName: acct.Phone, Status: coreaccount.StatusActive,
		}},
		Store:    pg.NewDiagnosticRepo(db),
		Clock:    clk,
		Location: time.UTC,
	})
	if err != nil {
		t.Fatalf("diag service: %v", err)
	}
	return &wtStack{
		t: t, ctx: ctx, admin: admin, db: db, schema: schema, clk: clk,
		sched: sched, worker: workerSvc, diag: diagSvc, signer: signer,
		acct: acct, ownerID: owner.ID, taskID: taskID, day: day, start: now,
	}
}

func dropWtSchema(t *testing.T, admin *pg.DB, schema string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if _, err := admin.Exec(ctx, "DROP SCHEMA IF EXISTS "+schema+" CASCADE"); err != nil {
		t.Logf("drop schema %s: %v", schema, err)
	}
}

// putWtWindow 预置当日窗口（相对当前测试时刻偏移）。
func (w *wtStack) putWtWindow(startIn, endIn time.Duration) {
	w.t.Helper()
	if _, err := w.sched.PutWindow(w.ctx, coreschedule.WindowRecord{
		SchoolTaskID: w.taskID, Day: w.day,
		Start: w.clk.Now().Add(startIn), End: w.clk.Now().Add(endIn),
		Source: "platform",
	}, 0); err != nil {
		w.t.Fatalf("put window: %v", err)
	}
}

// taskRow 读回账号当日任务行（经诊断只读仓储的真查询路径）。
func (w *wtStack) taskRow() (state, result string, runAt time.Time, attempts int) {
	w.t.Helper()
	rows, err := pg.NewDiagnosticRepo(w.db).TodayTasks(w.ctx, []string{w.acct.ID}, w.day)
	if err != nil {
		w.t.Fatalf("read task row: %v", err)
	}
	if len(rows) != 1 {
		w.t.Fatalf("task rows = %d, want 1", len(rows))
	}
	return rows[0].State, rows[0].Result, rows[0].RunAt, rows[0].Attempts
}

// Position 空（空点位列表）：行保持 pending，等待词在位，
// 诊断面给出 next_retry_at 与 wait 建议；手动重开清退避并重扫。
func TestWaitTargetEmptyPositionsStaysPending(t *testing.T) {
	w := newWtStack(t, &wtSigner{targets: nil})
	w.putWtWindow(-10*time.Minute, 50*time.Minute)
	if _, err := w.worker.Request(w.ctx, w.acct.ID, appaudit.Actor{UserID: w.ownerID, Role: coreauth.RoleUser, Label: "wt-e2e"}); err != nil {
		t.Fatalf("enqueue: %v", err)
	}
	if n, err := w.worker.Step(w.ctx, 1); err != nil || n != 1 {
		t.Fatalf("Step = (%d,%v), want (1,nil)", n, err)
	}
	state, result, runAt, attempts := w.taskRow()
	if state != string(corestatus.TaskPending) || attempts != 0 {
		t.Fatalf("state/attempts = %s/%d, want pending/0（不落终态、不计尝试）", state, attempts)
	}
	if result != "wait_target:1" {
		t.Fatalf("result = %q, want wait_target:1", result)
	}
	// 第 1 跳 = 5m×[0.5,1.5] = [2.5m,7.5m]。
	low, high := w.start.Add(150*time.Second), w.start.Add(450*time.Second)
	if runAt.Before(low) || runAt.After(high) {
		t.Fatalf("run_at = %v, want [%v,%v]", runAt, low, high)
	}

	// 诊断读面：state 仍 pending，next_retry_at 给出重扫时间，建议 wait。
	report, err := w.diag.Mine(w.ctx, w.ownerID)
	if err != nil {
		t.Fatalf("diag mine: %v", err)
	}
	if len(report.Items) != 1 {
		t.Fatalf("diag items = %d, want 1", len(report.Items))
	}
	item := report.Items[0]
	if item.State == nil || *item.State != string(corestatus.TaskPending) {
		t.Fatalf("diag state = %v, want pending", item.State)
	}
	if item.NextRetryAt == nil || !item.NextRetryAt.Equal(runAt) {
		t.Fatalf("diag next_retry_at = %v, want %v", item.NextRetryAt, runAt)
	}
	if item.Suggestion != "wait" || item.Result != "wait_target:1" {
		t.Fatalf("diag suggestion/result = %s/%q, want wait/wait_target:1", item.Suggestion, item.Result)
	}

	// 手动重开：退避清零（落点=现在），计数清空。
	if _, err := w.worker.Request(w.ctx, w.acct.ID, appaudit.Actor{UserID: w.ownerID, Role: coreauth.RoleUser, Label: "wt-e2e"}); err != nil {
		t.Fatalf("rearm request: %v", err)
	}
	state, result, runAt, _ = w.taskRow()
	if state != string(corestatus.TaskPending) || result != "" {
		t.Fatalf("after rearm state/result = %s/%q, want pending/空", state, result)
	}
	if !runAt.Equal(w.clk.Now()) {
		t.Fatalf("after rearm run_at = %v, want %v（退避清零）", runAt, w.clk.Now())
	}
}

// 窗口尾放不下下一跳：直接落 skipped(no_target) 终态。
func TestWaitTargetWindowEndLandsSkipped(t *testing.T) {
	w := newWtStack(t, &wtSigner{targets: nil})
	w.putWtWindow(-10*time.Minute, time.Minute) // 剩余 1 分钟：cap=18s < base 5m
	if _, err := w.worker.Request(w.ctx, w.acct.ID, appaudit.Actor{UserID: w.ownerID, Role: coreauth.RoleUser, Label: "wt-e2e"}); err != nil {
		t.Fatalf("enqueue: %v", err)
	}
	if _, err := w.worker.Step(w.ctx, 1); err != nil {
		t.Fatalf("Step: %v", err)
	}
	state, result, _, _ := w.taskRow()
	if state != string(corestatus.TaskSkipped) || result != "no_target" {
		t.Fatalf("state/result = %s/%q, want skipped/no_target", state, result)
	}
}

// K=3 耗尽：三轮退避重扫后落 skipped(no_target)，attempts 不计等待。
func TestWaitTargetKExhaustedLandsSkipped(t *testing.T) {
	w := newWtStack(t, &wtSigner{targets: nil})
	w.putWtWindow(-10*time.Minute, 3*time.Hour)
	if _, err := w.worker.Request(w.ctx, w.acct.ID, appaudit.Actor{UserID: w.ownerID, Role: coreauth.RoleUser, Label: "wt-e2e"}); err != nil {
		t.Fatalf("enqueue: %v", err)
	}
	for i := 0; i < 3; i++ {
		// 推过当前 run_at：模拟退避等待。
		w.clk.Advance(10 * time.Minute)
		if _, err := w.worker.Step(w.ctx, 1); err != nil {
			t.Fatalf("Step %d: %v", i, err)
		}
		state, result, _, attempts := w.taskRow()
		if state != string(corestatus.TaskPending) {
			t.Fatalf("Step %d: state = %s, want pending", i, state)
		}
		if want := fmt.Sprintf("wait_target:%d", i+1); result != want {
			t.Fatalf("Step %d: result = %q, want %q", i, result, want)
		}
		if attempts != 0 {
			t.Fatalf("Step %d: attempts = %d, want 0", i, attempts)
		}
	}
	// 第 4 次到点：K 已耗尽，落终态。
	w.clk.Advance(10 * time.Minute)
	if _, err := w.worker.Step(w.ctx, 1); err != nil {
		t.Fatalf("final Step: %v", err)
	}
	state, result, _, attempts := w.taskRow()
	if state != string(corestatus.TaskSkipped) || result != "no_target" {
		t.Fatalf("state/result = %s/%q, want skipped/no_target", state, result)
	}
	if attempts != 1 { // 终态落库计一次（Finish 口径），等待从未计
		t.Fatalf("attempts = %d, want 1", attempts)
	}
	if w.signer.signCalls != 0 {
		t.Fatalf("sign calls = %d, want 0（全程无点位，从未提交签到）", w.signer.signCalls)
	}
}

// State 2 的明确结论（no_target）：等待中途出现也直接终态，不进等待。
func TestWaitTargetExplicitNoTargetStaysTerminal(t *testing.T) {
	signer := &wtSigner{err: &contract.OutcomeError{
		Outcome: contract.OutcomeNoTarget, Message: "今日无需签到",
	}}
	w := newWtStack(t, signer)
	w.putWtWindow(-10*time.Minute, 50*time.Minute)
	if _, err := w.worker.Request(w.ctx, w.acct.ID, appaudit.Actor{UserID: w.ownerID, Role: coreauth.RoleUser, Label: "wt-e2e"}); err != nil {
		t.Fatalf("enqueue: %v", err)
	}
	if _, err := w.worker.Step(w.ctx, 1); err != nil {
		t.Fatalf("Step: %v", err)
	}
	state, result, _, _ := w.taskRow()
	if state != string(corestatus.TaskSkipped) || result != "no_target" {
		t.Fatalf("state/result = %s/%q, want skipped/no_target", state, result)
	}
}
