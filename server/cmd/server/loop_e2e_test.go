package main

// Loop 优雅停机的真库 e2e（派单 P7 判据）。
//
// # 功能
// 真库、真仓储、真执行循环：起 Loop → 在飞任务停在 Sign → Stop →
// 断言 Stop 等到在飞结算、DB 无 running 行、任务落 success。
// 模拟的真实动作：学生发起签到，任务被进程执行中时进程停机。
//
// # 口径
// 缺 DSN 时跳过并打横幅（包 TestMain 已有）。Signer 用阻塞替身：
// 平台侧协议由 conformance 套件负责，这里只钉停机顺序。

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appegress "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/egress"
	appworker "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/worker"
	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	corerate "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/rate"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	coreschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/school"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// loopSigner 是可阻塞的 Signer 替身：DiscoverTargets 给一个目标，
// Sign 挡在 release 上模拟在飞的平台调用。
type loopSigner struct {
	targets []contract.Target
	release chan struct{}
}

func (s *loopSigner) DiscoverTargets(context.Context, contract.DiscoverRequest) ([]contract.Target, error) {
	return s.targets, nil
}

func (s *loopSigner) Sign(context.Context, contract.SignRequest) (contract.Result, error) {
	<-s.release
	return contract.Result{Outcome: contract.OutcomeSuccess}, nil
}

// loopCreds 是固定凭据替身：协议面由阻塞 Signer 承担，凭据不必真实。
type loopCreds struct{}

func (loopCreds) Credential(context.Context, string) (contract.Credential, error) {
	return contract.Credential{Phone: "13800070001", Password: "x", DeviceCode: "d", PhoneModel: "m"}, nil
}

func TestLoopGracefulStopLeavesNoRunningRows(t *testing.T) {
	base := strings.TrimSpace(os.Getenv(authDSNEnv))
	if base == "" {
		t.Skip("SKIP: " + authDSNEnv + " is not set. Loop e2e did NOT run.")
	}
	ctx := context.Background()
	schema := "looptest_" + randomHex(8)

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
		dropLoopSchema(t, admin, schema)
		admin.Close()
		t.Fatalf("open schema db: %v", err)
	}
	t.Cleanup(func() {
		db.Close()
		dropLoopSchema(t, admin, schema)
		admin.Close()
	})
	if _, err := pg.Migrate(ctx, dsn); err != nil {
		t.Fatalf("migrate: %v", err)
	}

	// 种子：用户→学校→账号（审核激活）→出口→启用任务→当日窗口。
	// 写路径必须在显式事务里（tx.go 的判据）。
	users := pg.NewUserRepo(db)
	var owner, reviewer coreuser.Record
	err = db.InTx(ctx, func(ctx context.Context) error {
		var txErr error
		if owner, txErr = users.Create(ctx, coreuser.New{
			Email: "loop-owner@example.com", PasswordHash: "argon2id$test", Role: coreauth.RoleUser,
		}, 10000); txErr != nil {
			return txErr
		}
		reviewer, txErr = users.Create(ctx, coreuser.New{
			Email: "loop-reviewer@example.com", PasswordHash: "argon2id$test", Role: coreauth.RoleMaster,
		}, 10000)
		return txErr
	})
	if err != nil {
		t.Fatalf("create users: %v", err)
	}
	schools := pg.NewSchoolRepo(db)
	school, err := schools.Create(ctx, coreschool.New{Code: "loopschool", Name: "Loop School"})
	if err != nil {
		t.Fatalf("create school: %v", err)
	}
	accts := pg.NewAccountRepo(db)
	var acct coreaccount.Record
	err = db.InTx(ctx, func(ctx context.Context) error {
		var txErr error
		acct, txErr = accts.Create(ctx, coreaccount.New{
			SchoolID: school.ID, OwnerUserID: owner.ID, Phone: "13800070001",
		}, 10000, 10000)
		return txErr
	})
	if err != nil {
		t.Fatalf("create account: %v", err)
	}
	if _, err := accts.Review(ctx, corescope.Master(), acct.ID, true, "", reviewer.ID, (clock.System{}).Now()); err != nil {
		t.Fatalf("activate account: %v", err)
	}
	if _, err := db.Exec(ctx, "INSERT INTO egresses (identity) VALUES ($1)", "https://egress-loop-e2e"); err != nil {
		t.Fatalf("seed egress: %v", err)
	}

	sched := pg.NewScheduleRepo(db)
	taskID, err := sched.CreateSchoolTask(ctx, school.ID, "晨签")
	if err != nil {
		t.Fatalf("create school task: %v", err)
	}
	now := clock.System{}.Now()
	day := clock.BusinessDay(now, time.UTC)
	if _, err := sched.PutWindow(ctx, coreschedule.WindowRecord{
		SchoolTaskID: taskID, Day: day,
		Start: now.Add(-10 * time.Minute), End: now.Add(10 * time.Minute),
		Source: "platform",
	}, 0); err != nil {
		t.Fatalf("put window: %v", err)
	}

	egressSvc, err := appegress.NewService(appegress.Deps{
		Store: pg.NewEgressRepo(db), Clock: clock.System{},
	})
	if err != nil {
		t.Fatalf("egress service: %v", err)
	}
	signer := &loopSigner{
		targets: []contract.Target{{ID: "pos-1", Title: "东门"}},
		release: make(chan struct{}),
	}
	workerSvc, err := appworker.NewService(appworker.Deps{
		Store: sched, Creds: loopCreds{}, Signer: signer,
		Egress:  egressSvc,
		Limiter: corerate.NewLimiter(corerate.DefaultRate, 6),
		Clock:   clock.System{}, Location: time.UTC,
	})
	if err != nil {
		t.Fatalf("worker service: %v", err)
	}
	if _, err := workerSvc.Request(ctx, acct.ID, appaudit.Actor{UserID: "loop-test"}); err != nil {
		t.Fatalf("enqueue task: %v", err)
	}

	loop := appworker.NewLoop(workerSvc, workerSvc, 20*time.Millisecond, 1)
	if err := loop.Start(); err != nil {
		t.Fatalf("start loop: %v", err)
	}
	if err := loop.Start(); err == nil {
		t.Fatal("重复 Start 应返回错误（防 done 双关 panic）")
	}

	// 等任务被认领（state=running）：停机判据要先有在飞。
	waitFor(t, 3*time.Second, "task claimed", func() bool {
		n, err := workerSvc.CountRunning(ctx)
		return err == nil && n == 1
	})

	// 发起停机；在飞未放行前 Stop 不得返回。
	type stopResult struct {
		remain int64
		err    error
	}
	resCh := make(chan stopResult, 1)
	stopCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	go func() {
		n, err := loop.Stop(stopCtx)
		resCh <- stopResult{n, err}
	}()
	select {
	case r := <-resCh:
		t.Fatalf("Stop 在在飞结算前返回: remain=%d err=%v", r.remain, r.err)
	case <-time.After(300 * time.Millisecond):
	}

	// 放行在飞：Stop 必须等到结算后返回，且残余为 0。
	close(signer.release)
	var res stopResult
	select {
	case res = <-resCh:
	case <-time.After(5 * time.Second):
		t.Fatal("放行后 Stop 未返回")
	}
	if res.err != nil {
		t.Fatalf("Stop: %v", res.err)
	}
	if res.remain != 0 {
		t.Fatalf("Stop remain = %d, want 0", res.remain)
	}

	// 终态断言：无 running 行，任务已落 success。
	waitFor(t, 3*time.Second, "task settled", func() bool {
		state, err := db.QueryString(ctx,
			"SELECT state::text FROM sign_tasks WHERE account_id = $1", acct.ID)
		return err == nil && state == string(corestatus.TaskSuccess)
	})
	running, err := workerSvc.CountRunning(ctx)
	if err != nil {
		t.Fatalf("count running: %v", err)
	}
	if running != 0 {
		t.Fatalf("running rows = %d, want 0", running)
	}
}

func dropLoopSchema(t *testing.T, admin *pg.DB, schema string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if _, err := admin.Exec(ctx, "DROP SCHEMA IF EXISTS "+schema+" CASCADE"); err != nil {
		t.Logf("drop schema %s: %v", schema, err)
	}
}

// waitFor 轮询条件直到成立或超时。失败带用途名，便于定位。
func waitFor(t *testing.T, within time.Duration, what string, cond func() bool) {
	t.Helper()
	deadline := clock.System{}.Now().Add(within)
	for (clock.System{}).Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("%s not reached within %s", what, within)
}
