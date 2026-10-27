package pg_test

// db1 e2e：凭据错落账号状态，凭据更新恢复账号状态。
//
// 本文件钉住四条用户可见结果：
// 一次明确登录失败后，账号不再参与排期；管理面能按该状态查到账号；
// 改凭据把账号放回排期；只改手机号不放回。
// 判据全部落在真实 PostgreSQL 的行与审计列上。

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appworker "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/worker"
	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreegress "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/egress"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

const db1Day = "2026-10-29"

// db1At 是固定的执行时刻：窗口已开，计划行已到期。
func db1At() time.Time { return time.Date(2026, 10, 29, 6, 40, 0, 0, time.UTC) }

// db1NextDay 是次日同一时刻。
func db1NextDay() time.Time { return db1At().Add(24 * time.Hour) }

// db1Creds 返回任意明文凭据。本文件判的是结算后的库内状态。
type db1Creds struct{}

func (db1Creds) Credential(context.Context, string) (contract.Credential, error) {
	return contract.Credential{Phone: "13800000000", Password: "secret"}, nil
}

// db1Signer 在提交阶段返回凭据错结论。
type db1Signer struct{}

func (db1Signer) DiscoverTargets(context.Context, contract.DiscoverRequest) ([]contract.Target, error) {
	return []contract.Target{{ID: "pos-1", Title: "东门"}}, nil
}

func (db1Signer) Sign(context.Context, contract.SignRequest) (contract.Result, error) {
	return contract.Result{Outcome: contract.OutcomeCredentialBad}, nil
}

type db1Egress struct{}

func (db1Egress) Assign(context.Context, string) (coreegress.Record, error) {
	return coreegress.Record{ID: "eg-1", Identity: coreegress.Direct}, nil
}

type db1Limiter struct{}

func (db1Limiter) Admit(string, time.Time) bool               { return true }
func (db1Limiter) RetryAfter(string, time.Time) time.Duration { return 0 }

// db1SeedDueTask 建校、任务、活跃账号、开着的窗口与一条到期计划行。
func db1SeedDueTask(t *testing.T, e *testEnv, code, phone string) (schoolID, accountID, taskID string) {
	t.Helper()
	school := e.createSchool(code)
	taskID, err := e.sched.CreateSchoolTask(e.ctx, school.ID, "晨间签到")
	if err != nil {
		t.Fatalf("create school task: %v", err)
	}
	account := e.createAccount(school.ID, e.createUser(code+"-owner@example.com").ID, phone)
	e.activateAccount(account.ID)
	at := db1At()
	if _, err := e.sched.PutWindow(e.ctx, coreschedule.WindowRecord{
		SchoolTaskID: taskID, Day: db1Day,
		Start: at.Add(-10 * time.Minute), End: at.Add(20 * time.Minute),
		Source: string(contract.WindowPlatform),
	}, 0); err != nil {
		t.Fatalf("put window: %v", err)
	}
	if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{{
		AccountID: account.ID, SchoolTaskID: taskID, Day: db1Day, RunAt: at,
	}}); err != nil {
		t.Fatalf("insert plan: %v", err)
	}
	return school.ID, account.ID, taskID
}

// db1Worker 构造执行循环。Store 必须是带审计仓储的排期仓储。
func db1Worker(t *testing.T, store appworker.Store) *appworker.Service {
	t.Helper()
	svc, err := appworker.NewService(appworker.Deps{
		Store:    store,
		Creds:    db1Creds{},
		Signer:   db1Signer{},
		Egress:   db1Egress{},
		Limiter:  db1Limiter{},
		Clock:    coreclock.Fixed{T: db1At()},
		Location: time.UTC,
	})
	if err != nil {
		t.Fatalf("new worker service: %v", err)
	}
	return svc
}

// TestFinishCredentialInvalidRequiresAudit 钉住缺审计即报错：
// 未注入审计仓储的排期仓储不得静默写状态。
func TestFinishCredentialInvalidRequiresAudit(t *testing.T) {
	repo := pg.NewScheduleRepo(nil)
	err := repo.FinishCredentialInvalid(context.Background(), "school", "account", "task", db1Day, db1At())
	if !errors.Is(err, pg.ErrScheduleAuditMissing) {
		t.Fatalf("FinishCredentialInvalid without an audit repo = %v, want ErrScheduleAuditMissing", err)
	}
}

// TestCredentialInvalidWritesTaskAccountAndAudit 钉住一次结算的三件结果：
// 任务 failed(credential_invalid)、账号 credential_error、一条引擎审计行。
func TestCredentialInvalidWritesTaskAccountAndAudit(t *testing.T) {
	e := newEnv(t)
	schoolID, accountID, _ := db1SeedDueTask(t, e, "db1-settle", "13800021001")
	store := pg.NewScheduleRepoWithAudit(e.db, e.audits)
	svc := db1Worker(t, store)

	n, err := svc.Step(e.ctx, 1)
	if err != nil || n != 1 {
		t.Fatalf("Step = (%d,%v), want (1,nil)", n, err)
	}

	if got := e.scalarString("SELECT state::text FROM sign_tasks WHERE account_id = $1::uuid", accountID); got != string(corestatus.TaskFailed) {
		t.Fatalf("task state = %q, want failed", got)
	}
	if got := e.scalarString("SELECT result FROM sign_tasks WHERE account_id = $1::uuid", accountID); got != string(contract.OutcomeCredentialBad) {
		t.Fatalf("task result = %q, want credential_invalid", got)
	}
	if got := e.scalarString("SELECT status::text FROM accounts WHERE id = $1::uuid", accountID); got != string(coreaccount.StatusCredentialError) {
		t.Fatalf("account status = %q, want credential_error", got)
	}

	const wantDetail = `{"field":"status","from":"active","to":"credential_error"}`
	if got := e.scalarInt(`SELECT count(*) FROM audit_logs
		WHERE action = $1 AND actor_label = 'engine'
		  AND actor_user_id IS NULL AND actor_role IS NULL
		  AND school_id = $2::uuid AND target_id = $3 AND detail = $4`,
		string(coreaudit.ActionAccountCredentialError), schoolID, accountID, wantDetail); got != 1 {
		t.Fatalf("engine audit rows = %d, want 1", got)
	}
}

// TestCredentialErrorAccountIsNotClaimedNextDay 钉住验收 3：
// 账号进入 credential_error 后，次日到期任务不被认领。
func TestCredentialErrorAccountIsNotClaimedNextDay(t *testing.T) {
	e := newEnv(t)
	_, accountID, taskID := db1SeedDueTask(t, e, "db1-next", "13800021002")
	store := pg.NewScheduleRepoWithAudit(e.db, e.audits)
	svc := db1Worker(t, store)
	if _, err := svc.Step(e.ctx, 1); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if got := e.scalarString("SELECT status::text FROM accounts WHERE id = $1::uuid", accountID); got != string(coreaccount.StatusCredentialError) {
		t.Fatalf("precondition failed: account status = %q, want credential_error", got)
	}

	nextDay := db1NextDay()
	if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{{
		AccountID: accountID, SchoolTaskID: taskID, Day: nextDay.Format("2006-01-02"), RunAt: nextDay,
	}}); err != nil {
		t.Fatalf("insert next-day plan: %v", err)
	}
	claimed, err := e.sched.ClaimDue(e.ctx, nextDay.Add(time.Minute), 10)
	if err != nil {
		t.Fatalf("claim next day: %v", err)
	}
	if len(claimed) != 0 {
		t.Fatalf("claimed %d tasks for a credential_error account, want 0", len(claimed))
	}
	if got := e.scalarInt("SELECT count(*) FROM sign_tasks WHERE account_id = $1::uuid AND day = $2 AND state = 'pending'",
		accountID, nextDay.Format("2006-01-02")); got != 1 {
		t.Fatalf("next-day pending rows = %d, want 1 (the row must stay pending)", got)
	}
}

// TestCredentialInvalidSettleRollsBackOnAuditFailure 钉住同事务：
// 审计追加失败时，任务不落终态，账号不变状态。
func TestCredentialInvalidSettleRollsBackOnAuditFailure(t *testing.T) {
	e := newEnv(t)
	_, accountID, _ := db1SeedDueTask(t, e, "db1-rollback", "13800021003")
	store := pg.NewScheduleRepoWithAudit(e.db, e.audits)
	svc := db1Worker(t, store)

	undo := e.blockAuditWrites(t)
	_, err := svc.Step(e.ctx, 1)
	undo()
	if err == nil {
		t.Fatal("an audit write failure must fail the settlement, got nil")
	}
	if !strings.Contains(err.Error(), auditBlockMessage) {
		t.Fatalf("settlement failed for another reason: %v", err)
	}

	if got := e.scalarString("SELECT state::text FROM sign_tasks WHERE account_id = $1::uuid", accountID); got != string(corestatus.TaskRunning) {
		t.Fatalf("task state after the audit failure = %q, want running", got)
	}
	if got := e.scalarString("SELECT status::text FROM accounts WHERE id = $1::uuid", accountID); got != string(coreaccount.StatusActive) {
		t.Fatalf("account status after the audit failure = %q, want active", got)
	}
}

// db1CredentialErrorAccount 建一个带凭据的活跃账号，并直接置于 credential_error。
func db1CredentialErrorAccount(t *testing.T, u *usecaseEnv, code, phone string) (schoolID, accountID, ownerID string) {
	t.Helper()
	school := u.createSchool(code)
	owner := u.createUser(code + "-owner@example.com")
	account, err := u.accounts.Create(u.ctx, corescope.Master(), appaccount.CreateInput{
		SchoolID: school.ID, OwnerUserID: owner.ID, Phone: phone,
		Password: "old-secret", DeviceCode: "device-1", PhoneModel: "Model-X",
	}, u.actor)
	if err != nil {
		t.Fatalf("create account: %v", err)
	}
	u.activateAccount(account.ID)
	if _, err := u.db.Exec(u.ctx, "UPDATE accounts SET status = 'credential_error' WHERE id = $1::uuid", account.ID); err != nil {
		t.Fatalf("set credential_error: %v", err)
	}
	return school.ID, account.ID, owner.ID
}

// db1RecoveredAuditRows 数某账号的恢复审计行。
func (e *usecaseEnv) db1RecoveredAuditRows(accountID string) int64 {
	e.t.Helper()
	return e.scalarInt(`SELECT count(*) FROM audit_logs
		WHERE action = $1 AND target_id = $2
		  AND detail = '{"field":"status","from":"credential_error","to":"active"}'`,
		string(coreaudit.ActionAccountCredentialRecovered), accountID)
}

// TestCredentialUpdateRecoversCredentialError 钉住两条恢复形状：
// 属主改密码与管理员改密码都把账号放回 active，并各写一条恢复审计。
func TestCredentialUpdateRecoversCredentialError(t *testing.T) {
	cases := []struct {
		name  string
		code  string
		scope func(ownerID string) corescope.Scope
		actor func(u *usecaseEnv, ownerID, email string) appaudit.Actor
	}{
		{
			name:  "owner",
			code:  "db1-recover-owner",
			scope: func(ownerID string) corescope.Scope { return corescope.User(ownerID) },
			actor: func(u *usecaseEnv, ownerID, email string) appaudit.Actor {
				return appaudit.NewActor(coreauth.User{ID: ownerID, Email: email, Role: coreauth.RoleUser})
			},
		},
		{
			name:  "admin",
			code:  "db1-recover-admin",
			scope: func(string) corescope.Scope { return corescope.Master() },
			actor: func(u *usecaseEnv, _, _ string) appaudit.Actor { return u.actor },
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			u := newUsecaseEnv(t)
			_, accountID, ownerID := db1CredentialErrorAccount(t, u, tc.code, "1350005100"+map[string]string{"owner": "1", "admin": "2"}[tc.name])
			actor := tc.actor(u, ownerID, tc.code+"-owner@example.com")
			password := "new-secret"
			record, err := u.accounts.Update(u.ctx, tc.scope(ownerID), accountID,
				appaccount.UpdateInput{Password: &password}, actor)
			if err != nil {
				t.Fatalf("update password: %v", err)
			}
			if record.Status != coreaccount.StatusActive {
				t.Fatalf("returned status = %q, want active", record.Status)
			}
			if got := u.accountStatus(accountID); got != coreaccount.StatusActive {
				t.Fatalf("stored status = %q, want active", got)
			}
			if got := u.db1RecoveredAuditRows(accountID); got != 1 {
				t.Fatalf("recovery audit rows = %d, want 1", got)
			}
			if got := u.scalarString(`SELECT actor_label FROM audit_logs
				WHERE action = $1 AND target_id = $2`,
				string(coreaudit.ActionAccountCredentialRecovered), accountID); got != actor.Label {
				t.Fatalf("recovery actor label = %q, want %q", got, actor.Label)
			}
		})
	}
}

// TestPhoneOnlyUpdateKeepsCredentialError 钉住「只改手机号不恢复」。
func TestPhoneOnlyUpdateKeepsCredentialError(t *testing.T) {
	u := newUsecaseEnv(t)
	_, accountID, _ := db1CredentialErrorAccount(t, u, "db1-phone-only", "13500051003")
	phone := "13500051004"
	record, err := u.accounts.Update(u.ctx, corescope.Master(), accountID,
		appaccount.UpdateInput{Phone: &phone}, u.actor)
	if err != nil {
		t.Fatalf("update phone: %v", err)
	}
	if record.Status != coreaccount.StatusCredentialError {
		t.Fatalf("returned status = %q, want credential_error", record.Status)
	}
	if got := u.accountStatus(accountID); got != coreaccount.StatusCredentialError {
		t.Fatalf("stored status = %q, want credential_error", got)
	}
	if got := u.db1RecoveredAuditRows(accountID); got != 0 {
		t.Fatalf("recovery audit rows after a phone-only update = %d, want 0", got)
	}
}

// TestCredentialRecoveryRollsBackOnAuditFailure 钉住恢复路径的同事务：
// 审计追加失败时，凭据与账号状态一并回滚。
func TestCredentialRecoveryRollsBackOnAuditFailure(t *testing.T) {
	u := newUsecaseEnv(t)
	_, accountID, _ := db1CredentialErrorAccount(t, u, "db1-recover-rollback", "13500051005")
	undo := u.blockAuditWrites(t)
	password := "new-secret"
	_, err := u.accounts.Update(u.ctx, corescope.Master(), accountID,
		appaccount.UpdateInput{Password: &password}, u.actor)
	undo()
	if err == nil {
		t.Fatal("an audit write failure must fail the credential update, got nil")
	}
	if !strings.Contains(err.Error(), auditBlockMessage) {
		t.Fatalf("the update failed for another reason: %v", err)
	}
	if got := u.accountStatus(accountID); got != coreaccount.StatusCredentialError {
		t.Fatalf("account status after the audit failure = %q, want credential_error", got)
	}
	credential, err := u.accounts.SigningCredential(u.ctx, corescope.Master(), accountID)
	if err != nil {
		t.Fatalf("read credential: %v", err)
	}
	if credential.Password != "old-secret" {
		t.Fatalf("stored password after the audit failure = %q, want old-secret", credential.Password)
	}
}
