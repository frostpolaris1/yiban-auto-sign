package pg_test

// 自动审批凭据版本 CAS 的 e2e：仓库级谓词与用例级 TOCTOU。
//
// 判据不是调用顺序，而是数据库结果：
// 激活语句只在 credentials.version 仍等于核验版本时命中一行；
// 版本落后时账号留 pending、无 account.auto_approve 审计。

import (
	"bytes"
	"context"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// p2gAt 是本文件共用的固定时钟。
var p2gAt = time.Date(2026, 11, 20, 6, 0, 0, 0, time.UTC)

// createPendingAccount 走建号用例造一个带凭据的 pending 账号：
// 凭据行由 Cipher.Seal 写入，version 缺省 1。
func (e *usecaseEnv) createPendingAccount(schoolID, ownerID, phone string) coreaccount.Record {
	e.t.Helper()
	account, err := e.accounts.Create(e.ctx, corescope.Master(), appaccount.CreateInput{
		SchoolID: schoolID, OwnerUserID: ownerID, Phone: phone,
		Password: "p2g-secret", DeviceCode: "device-1", PhoneModel: "Model-X",
	}, e.actor)
	if err != nil {
		e.t.Fatalf("create pending account %s: %v", phone, err)
	}
	return account
}

// TestAccountSetStatusIfCredentialVersion 钉住仓库级版本谓词：
// credentials.version 仍等于核验版本才迁移；版本落后零行报 conflict，
// 状态与审计都不变。去掉 SQL 的 EXISTS 版本谓词时本用例红。
func TestAccountSetStatusIfCredentialVersion(t *testing.T) {
	u := newUsecaseEnv(t)
	school := u.createSchool("p2g-cas")
	owner := u.createUser("p2g-cas-owner@example.com")

	matched := u.createPendingAccount(school.ID, owner.ID, "13900139011")
	// ReplaceCredential 会 version+1：2 即核验读到的那一版。
	if _, err := u.db.Exec(u.ctx, "UPDATE credentials SET version = 2 WHERE account_id = $1::uuid", matched.ID); err != nil {
		t.Fatalf("raise credential version: %v", err)
	}

	record, err := u.accts.SetStatusIfCredentialVersion(u.ctx, corescope.Master(), matched.ID,
		coreaccount.StatusPending, coreaccount.StatusActive, 2, p2gAt, "")
	mustNotError(t, err, "activate with the matching credential version")
	if record.Status != coreaccount.StatusActive {
		t.Fatalf("record status = %q, want active", record.Status)
	}
	if got := u.accountStatus(matched.ID); got != coreaccount.StatusActive {
		t.Fatalf("stored status = %q, want active", got)
	}

	staleOwner := u.createUser("p2g-cas-owner2@example.com")
	stale := u.createPendingAccount(school.ID, staleOwner.ID, "13900139012")
	if _, err := u.db.Exec(u.ctx, "UPDATE credentials SET version = 2 WHERE account_id = $1::uuid", stale.ID); err != nil {
		t.Fatalf("raise stale credential version: %v", err)
	}

	// 越权作用域：版本谓词不绕过可见性，零行仍按 classifyMiss 报 not_found。
	outsider := u.createUser("p2g-cas-outsider@example.com")
	_, err = u.accts.SetStatusIfCredentialVersion(u.ctx, corescope.User(outsider.ID), stale.ID,
		coreaccount.StatusPending, coreaccount.StatusActive, 2, p2gAt, "")
	if got := codeOf(t, err); got != errs.CodeNotFound {
		t.Fatalf("foreign scope code = %q, want not_found", got)
	}
	if got := u.accountStatus(stale.ID); got != coreaccount.StatusPending {
		t.Fatalf("foreign scope status = %q, want pending", got)
	}

	_, err = u.accts.SetStatusIfCredentialVersion(u.ctx, corescope.Master(), stale.ID,
		coreaccount.StatusPending, coreaccount.StatusActive, 1, p2gAt, "")
	if got := codeOf(t, err); got != errs.CodeConflict {
		t.Fatalf("stale version code = %q, want conflict", got)
	}
	if got := u.accountStatus(stale.ID); got != coreaccount.StatusPending {
		t.Fatalf("stale account status = %q, want pending（版本不匹配不得迁移）", got)
	}
	if got := u.scalarInt("SELECT count(*) FROM audit_logs WHERE action = $1 AND target_id = $2",
		string(coreaudit.ActionAccountAutoApprove), stale.ID); got != 0 {
		t.Fatalf("auto approve audit rows = %d, want 0", got)
	}

	// 非正版本是调用错误，不是「版本不匹配」：报 validation 且不动行。
	_, err = u.accts.SetStatusIfCredentialVersion(u.ctx, corescope.Master(), stale.ID,
		coreaccount.StatusPending, coreaccount.StatusActive, 0, p2gAt, "")
	if got := codeOf(t, err); got != errs.CodeValidation {
		t.Fatalf("zero version code = %q, want validation", got)
	}
	if got := u.accountStatus(stale.ID); got != coreaccount.StatusPending {
		t.Fatalf("account status after validation = %q, want pending", got)
	}
}

// bumpingIdentity 在平台核验回调里把凭据版本 +1：
// 模拟「读凭据」与「激活」之间发生的 ReplaceCredential。
type bumpingIdentity struct {
	db   *pg.DB
	bump bool
}

func (f bumpingIdentity) Verify(ctx context.Context, req contract.VerifyRequest) (contract.Profile, error) {
	if f.bump {
		if _, err := f.db.Exec(ctx,
			"UPDATE credentials SET version = version + 1 WHERE account_id = $1::uuid", req.Account.ID); err != nil {
			return contract.Profile{}, err
		}
	}
	return contract.Profile{UniversityID: "P2G-U1"}, nil
}

// TestAutoApproveCredentialVersionChangedStaysPending 是 TOCTOU 的用例级证据：
// 平台核验通过，但核验期间凭据被替换——账号必须留 pending，
// 写 account.auto_check_failed(result=credential_changed)，且没有
// account.auto_approve 审计。激活仍只做纯状态 CAS 时本用例红。
func TestAutoApproveCredentialVersionChangedStaysPending(t *testing.T) {
	u := newUsecaseEnv(t)
	school := u.createSchool("p2g-tocttou")
	owner := u.createUser("p2g-tocttou-owner@example.com")
	if _, err := u.db.Exec(u.ctx,
		"UPDATE schools SET platform_university_id = 'P2G-U1' WHERE id = $1::uuid", school.ID); err != nil {
		t.Fatalf("bind the platform university: %v", err)
	}
	if _, err := u.db.Exec(u.ctx, "UPDATE settings SET value = 'true' WHERE key = 'auto_approve'"); err != nil {
		t.Fatalf("enable auto approve: %v", err)
	}

	cipher, err := appaccount.NewCipher(bytes.Repeat([]byte{7}, appaccount.CredentialKeyBytes))
	if err != nil {
		t.Fatalf("new credential cipher: %v", err)
	}
	svc, err := appaccount.NewService(appaccount.Deps{
		Accounts: u.accts,
		Schools:  u.testEnv.schools,
		Audit:    u.audits,
		Tx:       u.db,
		Clock:    coreclock.Fixed{T: p2gAt},
		Cipher:   cipher,
		Owner:    u.testEnv.users,
		Device:   stubDeviceReader{},
		Quota:    fixedQuota(10000),
		Settings: pg.NewSettingsRepo(u.db),
		Identity: bumpingIdentity{db: u.db, bump: true},
	})
	if err != nil {
		t.Fatalf("auto approve service: %v", err)
	}

	account, err := svc.Create(u.ctx, corescope.Master(), appaccount.CreateInput{
		SchoolID: school.ID, OwnerUserID: owner.ID, Phone: "13900139013",
		Password: "p2g-secret", DeviceCode: "device-1", PhoneModel: "Model-X",
		ConfirmDefaults: true,
	}, u.actor)
	if err != nil {
		t.Fatalf("create account: %v", err)
	}

	// 后台核验是异步的：有界等待失败审计落地。
	deadline := time.Now().Add(5 * time.Second)
	for {
		if u.scalarInt(`SELECT count(*) FROM audit_logs
			WHERE action = $1 AND target_id = $2 AND detail LIKE '%credential_changed%'`,
			string(coreaudit.ActionAccountAutoCheckFailed), account.ID) == 1 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("no credential_changed audit within the deadline; status = %q", u.accountStatus(account.ID))
		}
		time.Sleep(20 * time.Millisecond)
	}

	if got := u.accountStatus(account.ID); got != coreaccount.StatusPending {
		t.Fatalf("status = %q, want pending（核验期间凭据被替换不得激活）", got)
	}
	if got := u.scalarInt("SELECT count(*) FROM audit_logs WHERE action = $1 AND target_id = $2",
		string(coreaudit.ActionAccountAutoApprove), account.ID); got != 0 {
		t.Fatalf("auto approve audit rows = %d, want 0", got)
	}
	if got := u.scalarInt("SELECT version FROM credentials WHERE account_id = $1::uuid", account.ID); got != 2 {
		t.Fatalf("credential version = %d, want 2（核验期间的自增必须可见）", got)
	}
}
