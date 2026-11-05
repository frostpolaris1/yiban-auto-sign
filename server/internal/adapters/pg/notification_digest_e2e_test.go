package pg_test

// 0v8 C 每日签到失败摘要取数的真库 e2e。
//
// 覆盖：失败明细只取目标业务日、state='failed'、未软删账号；
// 排序稳定供用例「同账号取最新」；收件人只取 active 且未软删的
// master/admin；admin 带绑定校集合，无绑定校为空集合；
// 覆盖订阅按词表读出、词表外值报错。
// 摘要用例链路（scope、去重、账本唯一键、假 SMTP）在 cmd/server 的 e2e。

import (
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
)

// insertSignTask 直接写一行 sign_tasks。updated_at 只在 INSERT 指定：
// 表触发器会在 UPDATE 时覆盖它，故不能用 UPDATE 造时间差。
func (e *testEnv) insertSignTask(accountID, schoolTaskID, day, state, result string,
	attempts int, at time.Time) {
	e.t.Helper()
	if _, err := e.db.Exec(e.ctx, `
		INSERT INTO sign_tasks
		       (account_id, school_task_id, day, run_at, state, attempts, result, updated_at)
		VALUES ($1::uuid, $2::uuid, $3::date, $4::timestamptz, $5::sign_task_state, $6, $7, $8::timestamptz)`,
		accountID, schoolTaskID, day, at, state, attempts, result, at); err != nil {
		e.t.Fatalf("insert sign task %s/%s: %v", accountID, day, err)
	}
}

// 失败明细：目标日 failed、未软删账号才返回；其他日、非 failed、
// 软删账号都排除；同账号多任务保留多行，排序按 (account, updated_at)。
func TestNotificationDigestFailedAccounts(t *testing.T) {
	e := newEnv(t)
	school := e.createSchool("digest-fail")
	owner := e.createUser("digest-fail-owner@example.com")
	first := e.createAccount(school.ID, owner.ID, "13800000001")
	second := e.createAccount(school.ID, owner.ID, "13800000002")
	gone := e.createAccount(school.ID, owner.ID, "13800000003")
	taskOne, err := e.sched.CreateSchoolTask(e.ctx, school.ID, "摘要-任务一")
	if err != nil {
		t.Fatalf("create school task: %v", err)
	}
	taskTwo, err := e.sched.CreateSchoolTask(e.ctx, school.ID, "摘要-任务二")
	if err != nil {
		t.Fatalf("create school task: %v", err)
	}

	const day = "2026-10-29"
	early := time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)
	late := early.Add(5 * time.Minute)
	e.insertSignTask(first.ID, taskOne, day, string(corestatus.TaskFailed), "blocked", 2, early)
	e.insertSignTask(first.ID, taskTwo, day, string(corestatus.TaskFailed), "window_closed", 3, late)
	e.insertSignTask(second.ID, taskOne, day, string(corestatus.TaskSuccess), "", 1, early)
	e.insertSignTask(second.ID, taskTwo, "2026-10-28", string(corestatus.TaskFailed), "blocked", 4, early)
	e.insertSignTask(gone.ID, taskOne, day, string(corestatus.TaskFailed), "blocked", 1, early)
	if _, err := e.db.Exec(e.ctx,
		"UPDATE accounts SET deleted_at = now() WHERE id = $1::uuid", gone.ID); err != nil {
		t.Fatalf("soft delete account: %v", err)
	}

	repo := pg.NewNotificationDigestRepo(e.db)
	got, err := repo.FailedAccounts(e.ctx, day)
	if err != nil {
		t.Fatalf("FailedAccounts: %v", err)
	}
	if len(got) != 2 {
		t.Fatalf("明细 = %d, want 2（%+v）", len(got), got)
	}
	if got[0].AccountID != first.ID || got[0].Result != "blocked" ||
		got[0].Attempts != 2 || got[0].SchoolID != school.ID ||
		got[0].Phone != "13800000001" || !got[0].UpdatedAt.Equal(early) {
		t.Fatalf("第一行 = %+v", got[0])
	}
	if got[1].AccountID != first.ID || got[1].Result != "window_closed" ||
		got[1].Attempts != 3 || !got[1].UpdatedAt.Equal(late) {
		t.Fatalf("第二行 = %+v", got[1])
	}
	// 成功行、别的业务日、软删账号都不进目标日；空日返回空。
	other, err := repo.FailedAccounts(e.ctx, "2026-10-28")
	if err != nil {
		t.Fatalf("FailedAccounts(2026-10-28): %v", err)
	}
	if len(other) != 1 || other[0].AccountID != second.ID || other[0].Result != "blocked" {
		t.Fatalf("2026-10-28 明细 = %+v, want 仅该业务日的 1 行", other)
	}
	if rows, err := repo.FailedAccounts(e.ctx, "2026-10-27"); err != nil || len(rows) != 0 {
		t.Fatalf("空业务日明细 = %d (%v), want 0", len(rows), err)
	}
	if _, err := repo.FailedAccounts(e.ctx, "29/10/2026"); errs.From(err).Code != errs.CodeValidation {
		t.Fatalf("非法业务日 code = %v", errs.From(err).Code)
	}
	if _, err := repo.FailedAccounts(e.ctx, ""); errs.From(err).Code != errs.CodeValidation {
		t.Fatalf("空业务日 code = %v", errs.From(err).Code)
	}
}

// 收件人：master 全量、admin 带绑定校、无绑定校为空集合；
// 停用、软删、普通用户都不进；sign_failed_daily 覆盖随收件人读出。
func TestNotificationDigestSubscribers(t *testing.T) {
	e := newEnv(t)
	schoolA := e.createSchool("digest-sub-a")
	schoolB := e.createSchool("digest-sub-b")
	master := e.adminUser("digest-sub-master@example.com", coreauth.RoleMaster)
	admin := e.adminUser("digest-sub-admin@example.com", coreauth.RoleAdmin,
		schoolA.ID, schoolB.ID)
	unbound := e.adminUser("digest-sub-unbound@example.com", coreauth.RoleAdmin)
	disabled := e.adminUser("digest-sub-disabled@example.com", coreauth.RoleAdmin, schoolA.ID)
	if _, err := e.db.Exec(e.ctx,
		"UPDATE users SET status = 'disabled' WHERE id = $1::uuid", disabled.ID); err != nil {
		t.Fatalf("disable admin: %v", err)
	}
	deleted := e.adminUser("digest-sub-deleted@example.com", coreauth.RoleAdmin, schoolA.ID)
	if _, err := e.db.Exec(e.ctx,
		"UPDATE users SET deleted_at = now() WHERE id = $1::uuid", deleted.ID); err != nil {
		t.Fatalf("soft delete admin: %v", err)
	}
	e.createUser("digest-sub-plain@example.com")
	e.setPrefs(admin.ID, corenotify.Pref{
		Category: corenotify.CategorySignFailedDaily,
		Channel:  corenotify.ChannelEmail,
		Enabled:  false,
	})

	repo := pg.NewNotificationDigestRepo(e.db)
	got, err := repo.DigestSubscribers(e.ctx)
	if err != nil {
		t.Fatalf("DigestSubscribers: %v", err)
	}
	if len(got) != 3 {
		t.Fatalf("收件人 = %d, want 3（%+v）", len(got), got)
	}
	byID := make(map[string]corenotify.DigestSubscriber, len(got))
	for _, subscriber := range got {
		byID[subscriber.UserID] = subscriber
	}
	masterSub, ok := byID[master.ID]
	if !ok || !masterSub.Master || len(masterSub.Schools) != 0 ||
		len(masterSub.Overrides) != 0 || masterSub.Email != master.Email {
		t.Fatalf("master 收件人 = %+v", masterSub)
	}
	adminSub, ok := byID[admin.ID]
	if !ok || adminSub.Master || len(adminSub.Schools) != 2 {
		t.Fatalf("admin 收件人 = %+v", adminSub)
	}
	schools := map[string]bool{}
	for _, schoolID := range adminSub.Schools {
		schools[schoolID] = true
	}
	if !schools[schoolA.ID] || !schools[schoolB.ID] {
		t.Fatalf("admin 绑定校 = %v", adminSub.Schools)
	}
	pair := corenotify.Pair{
		Category: corenotify.CategorySignFailedDaily, Channel: corenotify.ChannelEmail,
	}
	if enabledValue, ok := adminSub.Overrides[pair]; !ok || enabledValue {
		t.Fatalf("覆盖订阅 = %+v", adminSub.Overrides)
	}
	unboundSub, ok := byID[unbound.ID]
	if !ok || unboundSub.Master || len(unboundSub.Schools) != 0 {
		t.Fatalf("无绑定校 admin = %+v", unboundSub)
	}
	for _, userID := range []string{disabled.ID, deleted.ID} {
		if _, ok := byID[userID]; ok {
			t.Fatalf("停用或软删用户 %s 进了收件人集合", userID)
		}
	}
}
