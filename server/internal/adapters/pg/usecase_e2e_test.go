package pg_test

// 用例层 e2e：受审计写的回滚证据、作用域判据与账号暂停形状。
//
// 本文件把 app 用例接到真实 PostgreSQL 适配器上。
// 判据是「审计写失败时目标行不落地」，不是调用顺序。
// 注入方式：给 audit_logs 装一个 BEFORE INSERT 触发器，令追加立即失败。
// 触发器在用例事务内触发，事务因此回滚；目标行必须保持写前状态。

import (
	"bytes"
	"context"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	appadmin "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/admin"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	applocation "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/location"
	appschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/school"
	appuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/user"
	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// usecaseEnv 把 app 用例接到同一个 per-test schema。
type usecaseEnv struct {
	*testEnv
	accounts *appaccount.Service
	schools  *appschool.Service
	admins   *appadmin.Service
	users    *appuser.Service
	location *applocation.Service
	actor    appaudit.Actor
}

// newUsecaseEnv 构造用例夹具。缺依赖即 Fatal。
func newUsecaseEnv(t *testing.T) *usecaseEnv {
	t.Helper()
	e := newEnv(t)
	now := time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)
	clk := coreclock.Fixed{T: now}

	cipher, err := appaccount.NewCipher(bytes.Repeat([]byte{7}, appaccount.CredentialKeyBytes))
	if err != nil {
		t.Fatalf("new credential cipher: %v", err)
	}
	accounts, err := appaccount.NewService(appaccount.Deps{
		Accounts: e.accts, Schools: e.schools, Audit: e.audits,
		Tx: e.db, Clock: clk, Cipher: cipher, Limit: 10000,
	})
	if err != nil {
		t.Fatalf("account service: %v", err)
	}
	schools, err := appschool.NewService(appschool.Deps{
		Schools: e.schools, Audit: e.audits, Tx: e.db, Clock: clk,
	})
	if err != nil {
		t.Fatalf("school service: %v", err)
	}
	admins, err := appadmin.NewService(appadmin.Deps{
		Admins: e.admins, Users: e.users, Schools: e.schools,
		Audit: e.audits, Tx: e.db, Clock: clk,
	})
	if err != nil {
		t.Fatalf("admin service: %v", err)
	}
	users, err := appuser.NewService(appuser.Deps{
		Users: e.users, Audit: e.audits, Tx: e.db, Clock: clk, Limit: 10000,
	})
	if err != nil {
		t.Fatalf("user service: %v", err)
	}
	location, err := applocation.NewService(applocation.Deps{
		Store: pg.NewLocationRepo(e.db), Accounts: accounts,
		Audit: e.audits, Tx: e.db, Clock: clk,
	})
	if err != nil {
		t.Fatalf("location service: %v", err)
	}

	master := e.createUser("usecase-master@example.com")
	return &usecaseEnv{
		testEnv:  e,
		accounts: accounts,
		schools:  schools,
		admins:   admins,
		users:    users,
		location: location,
		actor:    appaudit.NewActor(coreauth.User{ID: master.ID, Email: master.Email, Role: coreauth.RoleMaster}),
	}
}

// blockAuditWrites 让 audit_logs 的 INSERT 立即失败。
// 返回撤销函数；用例必须在断言前调用它，否则后续种子写不进审计。
func (e *testEnv) blockAuditWrites(t *testing.T) func() {
	t.Helper()
	fn := e.schema + ".test_block_audit"
	statements := []string{
		"DROP TRIGGER IF EXISTS test_block_audit ON audit_logs",
		"DROP FUNCTION IF EXISTS " + fn + "()",
		"CREATE FUNCTION " + fn + "() RETURNS trigger LANGUAGE plpgsql AS $body$ " +
			"BEGIN RAISE EXCEPTION 'audit writes are blocked by the test harness'; END $body$",
		"CREATE TRIGGER test_block_audit BEFORE INSERT ON audit_logs " +
			"FOR EACH ROW EXECUTE FUNCTION " + fn + "()",
	}
	for _, statement := range statements {
		if _, err := e.db.Exec(e.ctx, statement); err != nil {
			t.Fatalf("prepare the audit failure trigger: %v", err)
		}
	}
	return func() {
		if _, err := e.db.Exec(e.ctx, "DROP TRIGGER IF EXISTS test_block_audit ON audit_logs"); err != nil {
			t.Errorf("drop the audit failure trigger: %v", err)
		}
	}
}

// auditedWrite 是一条受审计写路径的回滚用例。
// setup 在装载故障前跑完种子，返回执行动作与「目标行未变」断言。
type auditedWrite struct {
	name  string
	setup func(t *testing.T, u *usecaseEnv) (write func() error, unchanged func())
}

// TestAuditWriteFailureRollsBackTargetRows 钉住九个受审计写路径的事务边界。
//
// 判据：审计追加失败时，用例返回 internal_error（HTTP 500），
// 且该路径写入的目标行一条都不落地。
// 把审计追加移出调用方事务时，目标行先提交——本用例红。
func TestAuditWriteFailureRollsBackTargetRows(t *testing.T) {
	u := newUsecaseEnv(t)
	for _, tc := range auditedWrites() {
		t.Run(tc.name, func(t *testing.T) {
			write, unchanged := tc.setup(t, u)
			undo := u.blockAuditWrites(t)
			err := write()
			undo()
			// 先查目标行：审计错误被吞掉时，这里直接给出「行已落地」的证据。
			unchanged()
			if err == nil {
				t.Fatal("an audit write failure must fail the use case, got nil")
			}
			if got := codeOf(t, err); got != errs.CodeInternal {
				t.Fatalf("audit failure maps to %q; want internal_error (HTTP 500): %v", got, err)
			}
		})
	}
}

// auditedWrites 列出九个受审计写路径。
func auditedWrites() []auditedWrite {
	return []auditedWrite{
		{
			name: "account.Create",
			setup: func(t *testing.T, u *usecaseEnv) (func() error, func()) {
				school := u.createSchool("rb-create")
				owner := u.createUser("rb-create-owner@example.com")
				const phone = "13500010001"
				return func() error {
						_, err := u.accounts.Create(u.ctx, corescope.Master(), appaccount.CreateInput{
							SchoolID:    school.ID,
							OwnerUserID: owner.ID,
							Phone:       phone,
							Password:    "secret",
							DeviceCode:  "device-1",
							PhoneModel:  "Model-X",
						}, u.actor)
						return err
					}, func() {
						if n := u.scalarInt("SELECT count(*) FROM accounts WHERE phone = $1", phone); n != 0 {
							t.Fatalf("account.Create left %d account rows after the audit failure", n)
						}
						if n := u.scalarInt("SELECT count(*) FROM credentials WHERE school_id = $1::uuid", school.ID); n != 0 {
							t.Fatalf("account.Create left %d credential rows after the audit failure", n)
						}
					}
			},
		},
		{
			name: "account.Update",
			setup: func(t *testing.T, u *usecaseEnv) (func() error, func()) {
				school := u.createSchool("rb-update")
				owner := u.createUser("rb-update-owner@example.com")
				account := u.createAccount(school.ID, owner.ID, "13500010002")
				newPhone := "13500010003"
				return func() error {
						_, err := u.accounts.Update(u.ctx, corescope.Master(), account.ID,
							appaccount.UpdateInput{Phone: &newPhone}, u.actor)
						return err
					}, func() {
						if got := u.scalarString("SELECT phone FROM accounts WHERE id = $1::uuid", account.ID); got != "13500010002" {
							t.Fatalf("account.Update left phone %q after the audit failure", got)
						}
					}
			},
		},
		{
			name: "account.Pause",
			setup: func(t *testing.T, u *usecaseEnv) (func() error, func()) {
				school := u.createSchool("rb-pause")
				owner := u.createUser("rb-pause-owner@example.com")
				account := u.createAccount(school.ID, owner.ID, "13500010004")
				u.activateAccount(account.ID)
				return func() error {
						_, err := u.accounts.Pause(u.ctx, corescope.Master(), account.ID, true, u.actor)
						return err
					}, func() {
						if got := u.scalarString("SELECT status::text FROM accounts WHERE id = $1::uuid", account.ID); got != string(coreaccount.StatusActive) {
							t.Fatalf("account.Pause left status %q after the audit failure", got)
						}
					}
			},
		},
		{
			name: "account.Delete",
			setup: func(t *testing.T, u *usecaseEnv) (func() error, func()) {
				school := u.createSchool("rb-delete")
				owner := u.createUser("rb-delete-owner@example.com")
				account := u.createAccount(school.ID, owner.ID, "13500010005")
				return func() error {
						return u.accounts.Delete(u.ctx, corescope.Master(), account.ID, u.actor)
					}, func() {
						if n := u.scalarInt("SELECT count(*) FROM accounts WHERE id = $1::uuid AND deleted_at IS NULL", account.ID); n != 1 {
							t.Fatalf("account.Delete left the row soft-deleted after the audit failure")
						}
					}
			},
		},
		{
			name: "school.Update",
			setup: func(t *testing.T, u *usecaseEnv) (func() error, func()) {
				school := u.createSchool("rb-school")
				name := "Renamed By A Failing Audit"
				return func() error {
						_, err := u.schools.Update(u.ctx, corescope.Master(), school.ID,
							appschool.Update{Name: &name}, u.actor)
						return err
					}, func() {
						if got := u.scalarString("SELECT name FROM schools WHERE id = $1::uuid", school.ID); got == name {
							t.Fatalf("school.Update left the new name %q after the audit failure", got)
						}
					}
			},
		},
		{
			name: "admin.Grant",
			setup: func(t *testing.T, u *usecaseEnv) (func() error, func()) {
				school := u.createSchool("rb-grant")
				admin := u.masterUser("rb-grant-admin@example.com")
				return func() error {
						_, err := u.admins.Grant(u.ctx, corescope.Master(), school.ID, admin.ID, u.actor)
						return err
					}, func() {
						if n := u.scalarInt("SELECT count(*) FROM admin_schools WHERE user_id = $1::uuid AND school_id = $2::uuid",
							admin.ID, school.ID); n != 0 {
							t.Fatalf("admin.Grant left %d binding rows after the audit failure", n)
						}
					}
			},
		},
		{
			name: "admin.Revoke",
			setup: func(t *testing.T, u *usecaseEnv) (func() error, func()) {
				school := u.createSchool("rb-revoke")
				admin := u.masterUser("rb-revoke-admin@example.com")
				if _, err := u.admins.Grant(u.ctx, corescope.Master(), school.ID, admin.ID, u.actor); err != nil {
					t.Fatalf("seed the binding: %v", err)
				}
				return func() error {
						return u.admins.Revoke(u.ctx, corescope.Master(), school.ID, admin.ID, u.actor)
					}, func() {
						if n := u.scalarInt("SELECT count(*) FROM admin_schools WHERE user_id = $1::uuid AND school_id = $2::uuid",
							admin.ID, school.ID); n != 1 {
							t.Fatalf("admin.Revoke removed the binding despite the audit failure")
						}
					}
			},
		},
		{
			name: "user.Create",
			setup: func(t *testing.T, u *usecaseEnv) (func() error, func()) {
				const email = "rb-user-create@example.com"
				return func() error {
						_, err := u.users.Create(u.ctx, corescope.Master(), appuser.CreateInput{
							Email: email, Password: "secret", Role: coreauth.RoleAdmin,
						}, u.actor)
						return err
					}, func() {
						if n := u.scalarInt("SELECT count(*) FROM users WHERE email = $1", email); n != 0 {
							t.Fatalf("user.Create left %d user rows after the audit failure", n)
						}
					}
			},
		},
		{
			name: "location.Set",
			setup: func(t *testing.T, u *usecaseEnv) (func() error, func()) {
				school := u.createSchool("rb-location")
				owner := u.createUser("rb-location-owner@example.com")
				account := u.createAccount(school.ID, owner.ID, "13500010006")
				lng, lat := 1.5, 2.5
				return func() error {
						_, err := u.location.Set(u.ctx, corescope.User(owner.ID), account.ID,
							applocation.Input{Mode: "fixed", Lng: &lng, Lat: &lat, Reason: "test"},
							u.actor)
						return err
					}, func() {
						if n := u.scalarInt("SELECT count(*) FROM account_location WHERE account_id = $1::uuid", account.ID); n != 0 {
							t.Fatalf("location.Set left %d preference rows after the audit failure", n)
						}
					}
			},
		},
	}
}

// masterUser 建一个角色为 master 的用户。admin.Grant 要求目标角色是 admin 或 master。
func (e *usecaseEnv) masterUser(email string) coreauth.User {
	e.t.Helper()
	rec := e.createUser(email)
	if _, err := e.db.Exec(e.ctx, "UPDATE users SET role = 'master' WHERE id = $1::uuid", rec.ID); err != nil {
		e.t.Fatalf("promote %s to master: %v", email, err)
	}
	return coreauth.User{ID: rec.ID, Email: rec.Email, Role: coreauth.RoleMaster}
}

// TestAccountListAndCredentialRejectForeignScope 钉住用例层把作用域原样下传。
//
// 判据：跨校 admin 与跨用户 user 作用域既看不到账号，也读不到凭据。
// 凭据行在库内存在，故「看不见」只能来自作用域过滤，不是缺行。
func TestAccountListAndCredentialRejectForeignScope(t *testing.T) {
	u := newUsecaseEnv(t)
	schoolA := u.createSchool("scope-a")
	schoolB := u.createSchool("scope-b")
	owner := u.createUser("scope-owner@example.com")
	other := u.createUser("scope-other@example.com")
	account := u.createAccount(schoolA.ID, owner.ID, "13500020001")

	// 先写一条真实凭据：若作用域被替换为 Master，读取会成功。
	cipher, err := appaccount.NewCipher(bytes.Repeat([]byte{7}, appaccount.CredentialKeyBytes))
	if err != nil {
		t.Fatalf("cipher: %v", err)
	}
	sealed, err := cipher.Seal(schoolA.ID, account.ID, appaccount.CredentialPayload{
		Password: "secret", DeviceCode: "device-1", PhoneModel: "Model-X",
	})
	if err != nil {
		t.Fatalf("seal: %v", err)
	}
	if err := u.db.InTx(u.ctx, func(ctx context.Context) error {
		return u.accts.InsertCredential(ctx, sealed)
	}); err != nil {
		t.Fatalf("insert credential: %v", err)
	}

	for _, tc := range []struct {
		name  string
		scope corescope.Scope
	}{
		{"admin of another school", corescope.Admin([]string{schoolB.ID})},
		{"another user", corescope.User(other.ID)},
		{"admin of no school", corescope.Admin(nil)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			page, err := u.accounts.List(u.ctx, tc.scope, appaccount.ListFilter{}, "", 10)
			if err != nil {
				t.Fatalf("list: %v", err)
			}
			if len(page.Items) != 0 {
				t.Fatalf("list returned %d accounts outside the scope", len(page.Items))
			}
			_, err = u.accounts.SigningCredential(u.ctx, tc.scope, account.ID)
			if got := codeOf(t, err); got != errs.CodeNotFound {
				t.Fatalf("SigningCredential outside the scope = %q; want not_found", got)
			}
		})
	}

	// 正例：属主能读到这条凭据，证明上面的 not_found 来自作用域。
	got, err := u.accounts.SigningCredential(u.ctx, corescope.User(owner.ID), account.ID)
	if err != nil {
		t.Fatalf("owner SigningCredential: %v", err)
	}
	if got.Password != "secret" || got.DeviceCode != "device-1" || got.PhoneModel != "Model-X" {
		t.Fatalf("credential payload = %+v", got)
	}
}

// TestPauseShapesOwnerVersusAdmin 钉住暂停的目标状态与恢复配对。
//
// 判据：属主暂停写 paused_user；管理员暂停写 paused_admin。
// 恢复只解自己的形状：属主不能解除管理员暂停，反之亦然。
//
// 删掉属主分支（一律 paused_admin）时，属主暂停走 CAS from=paused_admin
// 与 active 不匹配，返回冲突——本用例红。
func TestPauseShapesOwnerVersusAdmin(t *testing.T) {
	u := newUsecaseEnv(t)
	school := u.createSchool("pause-shape")
	owner := u.createUser("pause-shape-owner@example.com")
	account := u.createAccount(school.ID, owner.ID, "13500030001")
	u.activateAccount(account.ID)

	ownerScope := corescope.User(owner.ID)
	adminScope := corescope.Admin([]string{school.ID})

	paused, err := u.accounts.Pause(u.ctx, ownerScope, account.ID, true, u.actor)
	if err != nil {
		t.Fatalf("owner pause: %v", err)
	}
	if paused.Status != coreaccount.StatusPausedUser {
		t.Fatalf("owner pause wrote %q; want paused_user", paused.Status)
	}
	// 管理员不能解除属主暂停。
	_, err = u.accounts.Pause(u.ctx, adminScope, account.ID, false, u.actor)
	if got := codeOf(t, err); got != errs.CodeConflict {
		t.Fatalf("admin resume of paused_user = %q; want conflict", got)
	}
	if got := u.accountStatus(account.ID); got != coreaccount.StatusPausedUser {
		t.Fatalf("status after the rejected admin resume is %q; want paused_user", got)
	}
	resumed, err := u.accounts.Pause(u.ctx, ownerScope, account.ID, false, u.actor)
	if err != nil {
		t.Fatalf("owner resume: %v", err)
	}
	if resumed.Status != coreaccount.StatusActive {
		t.Fatalf("owner resume wrote %q; want active", resumed.Status)
	}

	paused, err = u.accounts.Pause(u.ctx, adminScope, account.ID, true, u.actor)
	if err != nil {
		t.Fatalf("admin pause: %v", err)
	}
	if paused.Status != coreaccount.StatusPausedAdmin {
		t.Fatalf("admin pause wrote %q; want paused_admin", paused.Status)
	}
	// 属主不能解除管理员暂停。
	_, err = u.accounts.Pause(u.ctx, ownerScope, account.ID, false, u.actor)
	if got := codeOf(t, err); got != errs.CodeConflict {
		t.Fatalf("owner resume of paused_admin = %q; want conflict", got)
	}
	if got := u.accountStatus(account.ID); got != coreaccount.StatusPausedAdmin {
		t.Fatalf("status after the rejected owner resume is %q; want paused_admin", got)
	}
	resumed, err = u.accounts.Pause(u.ctx, adminScope, account.ID, false, u.actor)
	if err != nil {
		t.Fatalf("admin resume: %v", err)
	}
	if resumed.Status != coreaccount.StatusActive {
		t.Fatalf("admin resume wrote %q; want active", resumed.Status)
	}
}

// accountStatus 读一个账号的状态。
func (e *usecaseEnv) accountStatus(id string) coreaccount.Status {
	e.t.Helper()
	return coreaccount.Status(e.scalarString("SELECT status::text FROM accounts WHERE id = $1::uuid", id))
}
