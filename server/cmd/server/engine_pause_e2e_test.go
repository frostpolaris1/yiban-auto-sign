package main

// 全局暂停机制的执行面真库 e2e（批 3a）。
//
// # 功能
// 真库、真仓储、真暂停门：暂停（settings 行 engine.paused=true）→
// Step 不消费到期行 → 恢复（false）→ Step 消费落 success。
// 同时钉住谓词 7 的执行侧：新构造的服务与 worker 读回暂停态（无进程态缓存）。
//
// # 口径
// 缺 DSN 时跳过并打横幅（包 TestMain 已有）。暂停查询与写面接真库
// settings 仓储与覆盖链解析器；Signer 用成功替身。

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appegress "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/egress"
	appsettings "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/settings"
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

// pauseSigner 是立即成功替身：执行面细节不是本文件判据。
type pauseSigner struct{}

func (pauseSigner) DiscoverTargets(context.Context, contract.DiscoverRequest) ([]contract.Target, error) {
	return []contract.Target{{ID: "pos-1", Title: "东门"}}, nil
}

func (pauseSigner) Sign(context.Context, contract.SignRequest) (contract.Result, error) {
	return contract.Result{Outcome: contract.OutcomeSuccess}, nil
}

// TestEnginePauseGatesConsumptionEndToEnd：暂停→行不消费→恢复→消费。
func TestEnginePauseGatesConsumptionEndToEnd(t *testing.T) {
	base := strings.TrimSpace(os.Getenv(authDSNEnv))
	if base == "" {
		t.Skip("SKIP: " + authDSNEnv + " is not set. Engine pause e2e did NOT run.")
	}
	ctx := context.Background()
	schema := "pause3a_" + randomHex(8)

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

	// 种子：用户→学校→账号（审核激活）→启用任务→当日窗口。
	users := pg.NewUserRepo(db)
	var owner, reviewer coreuser.Record
	err = db.InTx(ctx, func(ctx context.Context) error {
		var txErr error
		if owner, txErr = users.Create(ctx, coreuser.New{
			Email: "pause-owner@example.com", PasswordHash: "argon2id$test", Role: coreauth.RoleUser,
		}, 10000); txErr != nil {
			return txErr
		}
		reviewer, txErr = users.Create(ctx, coreuser.New{
			Email: "pause-reviewer@example.com", PasswordHash: "argon2id$test", Role: coreauth.RoleMaster,
		}, 10000)
		return txErr
	})
	if err != nil {
		t.Fatalf("create users: %v", err)
	}
	school, err := pg.NewSchoolRepo(db).Create(ctx, coreschool.New{Code: "pauseschool", Name: "Pause School"})
	if err != nil {
		t.Fatalf("create school: %v", err)
	}
	accts := pg.NewAccountRepo(db)
	var acct coreaccount.Record
	if err := db.InTx(ctx, func(ctx context.Context) error {
		var txErr error
		acct, txErr = accts.Create(ctx, coreaccount.New{
			SchoolID: school.ID, OwnerUserID: owner.ID, Phone: "13800070001",
		}, 10000)
		return txErr
	}); err != nil {
		t.Fatalf("create account: %v", err)
	}
	if _, err := accts.Review(ctx, corescope.Master(), acct.ID, true, "", reviewer.ID, (clock.System{}).Now()); err != nil {
		t.Fatalf("activate account: %v", err)
	}
	if _, err := db.Exec(ctx, "INSERT INTO egresses (identity) VALUES ($1)", "https://egress-pause-e2e"); err != nil {
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

	// 暂停门与写面接真库：覆盖链解析器（缺行=false）、settings 仓储。
	settingsRepo := pg.NewSettingsRepo(db)
	resolver, err := appsettings.NewResolver(settingsRepo, map[string]appsettings.SettingDomain{
		appsettings.KeyScheduleEdgeFrontSec: {Min: 0, Max: 300},
		appsettings.KeyScheduleEdgeBackSec:  {Min: 0, Max: 300},
	}, nil)
	if err != nil {
		t.Fatalf("NewResolver: %v", err)
	}
	pauseSvc, err := appsettings.NewService(appsettings.Deps{
		Store: pg.NewAnnouncementRepo(db), Audit: mustAuditRepo(t, db), Tx: db,
		Clock: clock.System{}, Schedule: settingsRepo, Resolver: resolver,
	})
	if err != nil {
		t.Fatalf("pause service: %v", err)
	}
	masterActor := appaudit.Actor{UserID: reviewer.ID, Role: coreauth.RoleMaster, Label: "p***@example.com"}

	egressSvc, err := appegress.NewService(appegress.Deps{
		Store: pg.NewEgressRepo(db), Clock: clock.System{},
	})
	if err != nil {
		t.Fatalf("egress service: %v", err)
	}
	workerSvc, err := appworker.NewService(appworker.Deps{
		Store: sched, Creds: loopCreds{}, Signer: pauseSigner{},
		Egress:  egressSvc,
		Limiter: corerate.NewLimiter(corerate.DefaultRate, 6),
		Clock:   clock.System{}, Location: time.UTC,
		// 暂停门与生产装配同形态：覆盖链每轮读一次，故障上抛。
		Paused: func(ctx context.Context) (bool, error) {
			return resolver.ResolveBool(ctx, appsettings.KeyEnginePaused, false)
		},
	})
	if err != nil {
		t.Fatalf("worker service: %v", err)
	}
	if _, err := workerSvc.Request(ctx, acct.ID, appaudit.Actor{UserID: "pause-test"}); err != nil {
		t.Fatalf("enqueue task: %v", err)
	}

	// 暂停：写开关为 true（master），Step 整轮不动，行保持 pending。
	if err := pauseSvc.SetEnginePaused(ctx, corescope.Master(), masterActor, true); err != nil {
		t.Fatalf("pause: %v", err)
	}
	if paused, err := pauseSvc.EnginePaused(ctx); err != nil || !paused {
		t.Fatalf("EnginePaused = (%v, %v), want (true, nil)", paused, err)
	}
	if n, err := workerSvc.Step(ctx, 5); err != nil || n != 0 {
		t.Fatalf("paused Step = (%d, %v), want (0, nil)", n, err)
	}
	if state := taskState(t, db, ctx, acct.ID); state != string(corestatus.TaskPending) {
		t.Fatalf("task state while paused = %q, want pending", state)
	}

	// 恢复：Step 消费到期行并结算 success。
	if err := pauseSvc.SetEnginePaused(ctx, corescope.Master(), masterActor, false); err != nil {
		t.Fatalf("resume: %v", err)
	}
	if n, err := workerSvc.Step(ctx, 5); err != nil || n != 1 {
		t.Fatalf("resumed Step = (%d, %v), want (1, nil)", n, err)
	}
	waitFor(t, 3*time.Second, "task settled after resume", func() bool {
		return taskState(t, db, ctx, acct.ID) == string(corestatus.TaskSuccess)
	})
}

// mustAuditRepo 构造审计仓储（暂停写入的审计行与 settings 同事务）。
func mustAuditRepo(t *testing.T, db *pg.DB) *pg.AuditRepo {
	t.Helper()
	repo, err := pg.NewAuditRepo(db, []byte("pause3a-e2e-audit-key-0000000000"))
	if err != nil {
		t.Fatalf("audit repo: %v", err)
	}
	return repo
}

// taskState 读账号当日任务行的当前状态。
func taskState(t *testing.T, db *pg.DB, ctx context.Context, accountID string) string {
	t.Helper()
	state, err := db.QueryString(ctx,
		"SELECT state::text FROM sign_tasks WHERE account_id = $1", accountID)
	if err != nil {
		t.Fatalf("read task state: %v", err)
	}
	return state
}
