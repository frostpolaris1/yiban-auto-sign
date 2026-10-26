package pg_test

// P5-C 真库 e2e：account_location 读写与约束；认领行携带账号属主。

import (
	"errors"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corelocation "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/location"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// fixedPref 构造一次 fixed 写入。
func fixedPref(accountID string, lng, lat float64) corelocation.Pref {
	return corelocation.Pref{
		AccountID: accountID, Mode: "fixed",
		Point: &contract.Point{Lng: lng, Lat: lat},
		Now:   time.Date(2026, 10, 27, 8, 0, 0, 0, time.UTC),
	}
}

// 读写回环：fixed 行带坐标与原因；auto 行坐标为 NULL 读回零值；
// 覆盖写替换整行；updated_by 记录操作者。
func TestLocationRepoRoundTrip(t *testing.T) {
	e := newEnv(t)
	repo := pg.NewLocationRepo(e.db)
	at := time.Date(2026, 10, 27, 8, 0, 0, 0, time.UTC)
	owner := e.createUser("loc-a@example.com")
	operator := e.createUser("loc-operator@example.com")
	school := e.createSchool("loc-a")
	acct := e.createAccount(school.ID, owner.ID, "13800030001")
	ctx := e.ctx

	if err := repo.Set(ctx, corelocation.Pref{
		AccountID: acct.ID, Mode: "fixed",
		Point:     &contract.Point{Lng: 116.4, Lat: 39.9},
		Reason:    "在校门口",
		UpdatedBy: operator.ID, Now: at,
	}); err != nil {
		t.Fatalf("set fixed: %v", err)
	}
	row, err := repo.Get(ctx, acct.ID)
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if row.Mode != "fixed" || row.Lng != 116.4 || row.Lat != 39.9 || row.Reason != "在校门口" {
		t.Fatalf("row = %+v", row)
	}
	if row.UpdatedBy != operator.ID {
		t.Fatalf("updated_by = %q, want %q", row.UpdatedBy, operator.ID)
	}
	if !row.UpdatedAt.Equal(at) {
		t.Fatalf("updated_at = %v", row.UpdatedAt)
	}

	// auto 覆盖：坐标清空读回零值。
	if err := repo.Set(ctx, corelocation.Pref{AccountID: acct.ID, Mode: "auto", Now: at.Add(time.Minute)}); err != nil {
		t.Fatalf("set auto: %v", err)
	}
	row, err = repo.Get(ctx, acct.ID)
	if err != nil || row.Mode != "auto" || row.Lng != 0 || row.Lat != 0 {
		t.Fatalf("auto row = (%+v,%v)", row, err)
	}

	// 无行：ErrNotFound（用合法但不存在 的 uuid）。
	if _, err := repo.Get(ctx, "00000000-0000-0000-0000-0000000000aa"); !errors.Is(err, errs.ErrNotFound) {
		t.Fatalf("missing get = %v, want not_found", err)
	}
}

// 存储层 fail-closed：fixed 缺经纬、未知账号、未知操作者都被拒。
func TestLocationRepoRejectsBrokenRows(t *testing.T) {
	at := time.Date(2026, 10, 27, 8, 0, 0, 0, time.UTC)
	e := newEnv(t)
	repo := pg.NewLocationRepo(e.db)
	owner := e.createUser("loc-b@example.com")
	school := e.createSchool("loc-b")
	acct := e.createAccount(school.ID, owner.ID, "13800030002")
	ctx := e.ctx

	broken := fixedPref(acct.ID, 116.4, 39.9)
	broken.Point = nil // fixed 无坐标：CHECK 必须拒绝
	if err := repo.Set(ctx, broken); err == nil {
		t.Fatal("fixed 缺坐标应被 CHECK 拒绝")
	}
	if err := repo.Set(ctx, fixedPref("00000000-0000-0000-0000-000000000000", 1, 1)); err == nil {
		t.Fatal("未知账号应被外键拒绝")
	}
	missingOperator := fixedPref(acct.ID, 1, 1)
	missingOperator.UpdatedBy = "00000000-0000-0000-0000-000000000000"
	if err := repo.Set(ctx, missingOperator); err == nil {
		t.Fatal("未知操作者应被外键拒绝")
	}
	if err := repo.Set(ctx, corelocation.Pref{AccountID: acct.ID, Mode: "gps", Now: at.Add(time.Minute)}); err == nil {
		t.Fatal("非法 mode 应被 CHECK 拒绝")
	}
}

// 认领行与手动签到建行都携带账号属主（worker 选图靠它）。
func TestTaskRowsCarryOwner(t *testing.T) {
	e := newEnv(t)
	owner := e.createUser("loc-owner@example.com")
	school := e.createSchool("loc-c")
	task, err := e.sched.CreateSchoolTask(e.ctx, school.ID, "晨签")
	if err != nil {
		t.Fatalf("task: %v", err)
	}
	acct := e.createAccount(school.ID, owner.ID, "13800030003")
	e.activateAccount(acct.ID)
	day := "2026-10-29"
	at := time.Date(2026, 10, 29, 6, 40, 0, 0, time.UTC)
	if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{
		{AccountID: acct.ID, SchoolTaskID: task, Day: day, RunAt: at},
	}); err != nil {
		t.Fatalf("insert: %v", err)
	}

	claimed, err := e.sched.ClaimDue(e.ctx, at.Add(time.Minute), 10)
	if err != nil || len(claimed) != 1 {
		t.Fatalf("claim = (%d,%v)", len(claimed), err)
	}
	if claimed[0].OwnerUserID != owner.ID {
		t.Fatalf("ClaimDue 属主 = %q, want %q", claimed[0].OwnerUserID, owner.ID)
	}

	// EnsureTask（手动签到建行）同样带属主。
	row, _, err := e.sched.EnsureTask(e.ctx, acct.ID, task, "2026-10-30", at)
	if err != nil {
		t.Fatalf("ensure: %v", err)
	}
	if row.OwnerUserID != owner.ID {
		t.Fatalf("EnsureTask 属主 = %q, want %q", row.OwnerUserID, owner.ID)
	}
}
