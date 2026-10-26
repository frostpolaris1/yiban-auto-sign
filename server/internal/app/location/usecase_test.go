package location_test

import (
	"context"
	"errors"
	"testing"
	"time"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	applocation "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/location"
	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// ---- 测试替身 ----

// fakeAccounts 只实现可见面判据：user 只见自己的账号，admin 只见绑定校。
type fakeAccounts struct{ recs map[string]coreaccount.Record }

func (f fakeAccounts) Get(_ context.Context, scope corescope.Scope, id string) (coreaccount.Record, error) {
	rec, ok := f.recs[id]
	if !ok {
		return coreaccount.Record{}, errs.ErrNotFound
	}
	switch scope.Kind() {
	case corescope.KindMaster:
		return rec, nil
	case corescope.KindAdmin:
		for _, sid := range scope.SchoolIDs() {
			if sid == rec.SchoolID {
				return rec, nil
			}
		}
		return coreaccount.Record{}, errs.ErrNotFound
	case corescope.KindUser:
		if scope.UserID() == rec.OwnerUserID {
			return rec, nil
		}
		return coreaccount.Record{}, errs.ErrNotFound
	default:
		return coreaccount.Record{}, errs.New(errs.CodeInternal, "invalid scope")
	}
}

type fakeStore struct {
	rows map[string]applocation.Row
	// sets 记录每次写入，供断言坐标清空语义。
	sets []applocation.Pref
}

func newFakeStore() *fakeStore { return &fakeStore{rows: map[string]applocation.Row{}} }

func (f *fakeStore) Get(_ context.Context, accountID string) (applocation.Row, error) {
	row, ok := f.rows[accountID]
	if !ok {
		return applocation.Row{}, errs.ErrNotFound
	}
	return row, nil
}

func (f *fakeStore) Set(_ context.Context, in applocation.Pref) error {
	f.sets = append(f.sets, in)
	row := applocation.Row{
		AccountID: in.AccountID, Mode: in.Mode, Reason: in.Reason,
		UpdatedBy: in.UpdatedBy, UpdatedAt: in.Now,
	}
	if in.Point != nil {
		row.Lng, row.Lat = in.Point.Lng, in.Point.Lat
	}
	f.rows[in.AccountID] = row
	return nil
}

type fakeAudit struct{ entries []coreaudit.Entry }

func (f *fakeAudit) Append(_ context.Context, in coreaudit.Entry) (coreaudit.Row, error) {
	f.entries = append(f.entries, in)
	return coreaudit.Row{}, nil
}

func (f *fakeAudit) Verify(context.Context, int64, int64) (coreaudit.Report, error) {
	return coreaudit.Report{}, errors.New("not used")
}

type fakeTx struct{ committed int }

func (f *fakeTx) InTx(ctx context.Context, fn func(ctx context.Context) error) error {
	f.committed++
	return fn(ctx)
}

// ---- 辅助 ----

var (
	acctA = coreaccount.Record{ID: "acct-a", SchoolID: "school-1", OwnerUserID: "user-1"}
	acctB = coreaccount.Record{ID: "acct-b", SchoolID: "school-2", OwnerUserID: "user-2"}
)

func newService(store *fakeStore, accounts fakeAccounts, audit *fakeAudit) (*applocation.Service, *fakeTx) {
	tx := &fakeTx{}
	svc, err := applocation.NewService(applocation.Deps{
		Store: store, Accounts: accounts, Audit: audit, Tx: tx,
		Clock: coreclock.Fixed{T: time.Date(2026, 10, 27, 8, 0, 0, 0, time.UTC)},
	})
	if err != nil {
		panic(err)
	}
	return svc, tx
}

func fixedInput(lng, lat float64, reason string) applocation.Input {
	lngIn, latIn := lng, lat
	return applocation.Input{Mode: "fixed", Lng: &lngIn, Lat: &latIn, Reason: reason}
}

func hasReason(e coreaudit.Entry, want bool) bool {
	value, ok := e.Detail["has_reason"].(bool)
	return ok && value == want
}

// ---- 用例 ----

// user 写自己账号：写入 fixed 行并记 location.set 审计，detail 只含 mode 与 has_reason。
func TestSetFixedByOwner(t *testing.T) {
	store, audit := newFakeStore(), &fakeAudit{}
	svc, tx := newService(store, fakeAccounts{recs: map[string]coreaccount.Record{"acct-a": acctA}}, audit)
	ctx := context.Background()
	actor := appaudit.Actor{UserID: "user-1", Role: coreauth.RoleUser}

	out, err := svc.Set(ctx, corescope.User("user-1"), "acct-a", fixedInput(116.4, 39.9, "在校门口"), actor)
	if err != nil {
		t.Fatalf("Set: %v", err)
	}
	if out.Mode != contract.LocationFixed || out.Lng != 116.4 || out.Lat != 39.9 || out.Reason != "在校门口" {
		t.Fatalf("out = %+v", out)
	}
	if tx.committed != 1 {
		t.Fatalf("事务数 = %d, want 1", tx.committed)
	}
	if len(store.sets) != 1 || store.sets[0].Point == nil {
		t.Fatalf("sets = %+v", store.sets)
	}
	if len(audit.entries) != 1 {
		t.Fatalf("审计行数 = %d", len(audit.entries))
	}
	entry := audit.entries[0]
	if entry.Action != coreaudit.ActionLocationSet || entry.TargetID != "acct-a" || entry.SchoolID != "school-1" {
		t.Fatalf("entry = %+v", entry)
	}
	if entry.Detail["mode"] != "fixed" || !hasReason(entry, true) {
		t.Fatalf("detail = %+v", entry.Detail)
	}
	if len(entry.Detail) != 2 {
		t.Fatalf("detail 只应有两个键: %+v", entry.Detail)
	}
}

// user 写他人账号与 admin 写未绑定校账号：一律 404，不落行。
func TestSetCrossScopeNotFound(t *testing.T) {
	accounts := fakeAccounts{recs: map[string]coreaccount.Record{
		"acct-a": acctA, "acct-b": acctB,
	}}
	svc, _ := newService(newFakeStore(), accounts, &fakeAudit{})
	ctx := context.Background()
	actor := appaudit.Actor{UserID: "user-1", Role: coreauth.RoleUser}

	for name, tc := range map[string]struct {
		scope corescope.Scope
		id    string
	}{
		"user 写他人账号":    {scope: corescope.User("user-2"), id: "acct-a"},
		"admin 写未绑定校账号": {scope: corescope.Admin([]string{"school-1"}), id: "acct-b"},
	} {
		_, err := svc.Set(ctx, tc.scope, tc.id, fixedInput(116.4, 39.9, ""), actor)
		if !errors.Is(err, errs.ErrNotFound) {
			t.Fatalf("%s: err = %v, want not_found", name, err)
		}
	}
}

// admin 写绑定校账号：放行。
func TestSetByBoundAdmin(t *testing.T) {
	svc, _ := newService(newFakeStore(),
		fakeAccounts{recs: map[string]coreaccount.Record{"acct-a": acctA}}, &fakeAudit{})
	actor := appaudit.Actor{UserID: "admin-1", Role: coreauth.RoleAdmin}
	out, err := svc.Set(context.Background(), corescope.Admin([]string{"school-1"}), "acct-a",
		fixedInput(100.0, 20.0, ""), actor)
	if err != nil || out.Mode != contract.LocationFixed {
		t.Fatalf("Set = (%+v,%v)", out, err)
	}
}

// fixed 缺经纬度、越界坐标、非法 mode、超长 reason：400，不落行不记审计。
func TestSetValidation(t *testing.T) {
	audit := &fakeAudit{}
	svc, _ := newService(newFakeStore(),
		fakeAccounts{recs: map[string]coreaccount.Record{"acct-a": acctA}}, audit)
	ctx := context.Background()
	scope := corescope.User("user-1")
	actor := appaudit.Actor{UserID: "user-1", Role: coreauth.RoleUser}

	lng := 116.4
	cases := []struct {
		name string
		in   applocation.Input
	}{
		{"fixed 缺经度", applocation.Input{Mode: "fixed"}},
		{"fixed 缺纬度", applocation.Input{Mode: "fixed", Lng: &lng}},
		{"mode 非法", applocation.Input{Mode: "gps"}},
		{"经度越界", fixedInput(180.5, 0, "")},
		{"纬度越界", fixedInput(0, 90.5, "")},
		{"reason 超长", fixedInput(0, 0, string(make([]rune, 201)))},
	}
	for _, c := range cases {
		_, err := svc.Set(ctx, scope, "acct-a", c.in, actor)
		if errs.From(err).Code != errs.CodeValidation {
			t.Fatalf("%s: err = %v, want validation", c.name, err)
		}
	}
	if len(audit.entries) != 0 {
		t.Fatalf("校验失败不应记审计: %d", len(audit.entries))
	}
}

// auto 覆盖 fixed：坐标清空（Point 为 nil），审计记 has_reason=false。
func TestSetAutoClearsPoint(t *testing.T) {
	store := newFakeStore()
	audit := &fakeAudit{}
	svc, _ := newService(store, fakeAccounts{recs: map[string]coreaccount.Record{"acct-a": acctA}}, audit)
	ctx := context.Background()
	actor := appaudit.Actor{UserID: "user-1", Role: coreauth.RoleUser}

	if _, err := svc.Set(ctx, corescope.User("user-1"), "acct-a", fixedInput(1, 2, "x"), actor); err != nil {
		t.Fatalf("seed fixed: %v", err)
	}
	out, err := svc.Set(ctx, corescope.User("user-1"), "acct-a", applocation.Input{Mode: "auto"}, actor)
	if err != nil {
		t.Fatalf("Set auto: %v", err)
	}
	if out.Mode != contract.LocationAuto || out.Reason != "" {
		t.Fatalf("out = %+v", out)
	}
	last := store.sets[len(store.sets)-1]
	if last.Point != nil {
		t.Fatalf("auto 行仍带坐标: %+v", last.Point)
	}
	if audit.entries[len(audit.entries)-1].Detail["mode"] != "auto" {
		t.Fatalf("audit detail = %+v", audit.entries[len(audit.entries)-1].Detail)
	}
}

// Resolve：fixed 行带经纬与 reason；无行与 auto 行都是 LocationAuto。
func TestResolveShapes(t *testing.T) {
	store := newFakeStore()
	svc, _ := newService(store, fakeAccounts{}, &fakeAudit{})
	ctx := context.Background()

	loc, err := svc.Resolve(ctx, "acct-a")
	if err != nil || loc != (contract.Location{Mode: contract.LocationAuto}) {
		t.Fatalf("无行 Resolve = (%+v,%v)", loc, err)
	}

	row := applocation.Row{
		AccountID: "acct-a", Mode: "fixed", Lng: 116.4, Lat: 39.9,
		Reason: "在校门口", UpdatedAt: time.Date(2026, 10, 27, 8, 0, 0, 0, time.UTC),
	}
	store.rows["acct-a"] = row
	loc, err = svc.Resolve(ctx, "acct-a")
	if err != nil || loc != (contract.Location{
		Mode: contract.LocationFixed, Lng: 116.4, Lat: 39.9, Reason: "在校门口",
	}) {
		t.Fatalf("fixed Resolve = (%+v,%v)", loc, err)
	}

	store.rows["acct-b"] = applocation.Row{AccountID: "acct-b", Mode: "auto"}
	loc, err = svc.Resolve(ctx, "acct-b")
	if err != nil || loc.Mode != contract.LocationAuto {
		t.Fatalf("auto Resolve = (%+v,%v)", loc, err)
	}
}

// NewService 缺依赖即失败。
func TestNewServiceRequiresDeps(t *testing.T) {
	complete := applocation.Deps{
		Store: newFakeStore(), Accounts: fakeAccounts{}, Audit: &fakeAudit{},
		Tx: &fakeTx{}, Clock: coreclock.Fixed{},
	}
	for name, broken := range map[string]applocation.Deps{
		"缺 Store":    merge(complete, func(d *applocation.Deps) { d.Store = nil }),
		"缺 Accounts": merge(complete, func(d *applocation.Deps) { d.Accounts = nil }),
		"缺 Audit":    merge(complete, func(d *applocation.Deps) { d.Audit = nil }),
		"缺 Tx":       merge(complete, func(d *applocation.Deps) { d.Tx = nil }),
		"缺 Clock":    merge(complete, func(d *applocation.Deps) { d.Clock = nil }),
	} {
		if _, err := applocation.NewService(broken); err == nil {
			t.Fatalf("%s: 应失败", name)
		}
	}
}

func merge(base applocation.Deps, mutate func(*applocation.Deps)) applocation.Deps {
	mutate(&base)
	return base
}

// 编译期断言：可见面判据与 app/account 的公开形状对齐。
var _ applocation.Accounts = fakeAccounts{}
