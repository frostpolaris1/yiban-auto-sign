package pg_test

// P2-8：用户列表先裁页，再按 owner 聚合账号。
//
// # 功能
// 分组过滤只依赖相关 EXISTS；列表先 LIMIT 本页再 LEFT JOIN LATERAL 聚合，
// 空页不得触达 accounts；Counts 的四个计数语义与分页、分组前一致。
//
// # 归属
// 测试载体。被测对象是 pg.UserRepo.List / Counts。

import (
	"strings"
	"testing"
	"time"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// userListFixture 是分组与计数的固定形状：
// master/normal/deletedAcct 各 1 个 live 账号；pending 有 pending+rejected；
// vacant 无账号；deleted 是软删用户（不计入前三个分组）。
type userListFixture struct {
	master      coreuser.Record
	pending     coreuser.Record
	normal      coreuser.Record
	deletedAcct coreuser.Record
	vacant      coreuser.Record
	deleted     coreuser.Record
}

// seedUserListFixture 造数并把 created_at 铺成 master > pending > normal >
// deletedAcct > vacant > deleted。顺序由数据显式给定，不依赖 now() 的微秒差。
func seedUserListFixture(t *testing.T, e *testEnv) userListFixture {
	t.Helper()
	f := userListFixture{
		master:      e.adminUser("ulist-master@example.com", coreauth.RoleMaster),
		pending:     e.createUser("ulist-pending@example.com"),
		normal:      e.createUser("ulist-normal@example.com"),
		deletedAcct: e.createUser("ulist-deleted-acct@example.com"),
		vacant:      e.createUser("ulist-vacant@example.com"),
		deleted:     e.createUser("ulist-deleted@example.com"),
	}

	school := e.createSchool("ulist")
	activate := func(owner, phone string) {
		acc := e.createAccount(school.ID, owner, phone)
		if _, err := e.accts.Review(e.ctx, corescope.Master(), acc.ID, true, "", f.master.ID, time.Now()); err != nil {
			t.Fatalf("activate %s: %v", phone, err)
		}
	}
	activate(f.master.ID, "13800000001")
	activate(f.normal.ID, "13800000002")
	activate(f.deletedAcct.ID, "13800000003")

	// pending：一个留在 pending，另一个驳回 → account=2/pending=1/review=2。
	e.createAccount(school.ID, f.pending.ID, "13800000004")
	rejected := e.createAccount(school.ID, f.pending.ID, "13800000005")
	if _, err := e.accts.Review(e.ctx, corescope.Master(), rejected.ID, false, "bad", f.master.ID, time.Now()); err != nil {
		t.Fatalf("reject 13800000005: %v", err)
	}

	// deletedAcct：一个 active，一个软删 → 计数只看 live 行。
	soft := e.createAccount(school.ID, f.deletedAcct.ID, "13800000006")
	e.exec("UPDATE accounts SET deleted_at = now(), deleted_by = $1::uuid WHERE id = $2::uuid",
		f.master.ID, soft.ID)

	// deleted：软删用户 → 只计入 Deleted。
	e.exec("UPDATE users SET deleted_at = now(), deleted_by = $1::uuid WHERE id = $2::uuid",
		f.master.ID, f.deleted.ID)

	users := []coreuser.Record{f.master, f.pending, f.normal, f.deletedAcct, f.vacant, f.deleted}
	base := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)
	for i, u := range users {
		e.exec("UPDATE users SET created_at = $1 WHERE id = $2::uuid",
			base.Add(time.Duration(len(users)-i)*time.Minute), u.ID)
	}
	return f
}

// wantUserRow 是列表行的可见口径：id 与三个账号计数。
type wantUserRow struct {
	id      string
	account int
	pending int
	review  int
}

func assertUserRows(t *testing.T, label string, got []coreuser.Record, want []wantUserRow) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("%s: got %d rows, want %d", label, len(got), len(want))
	}
	for i := range want {
		row := got[i]
		if row.ID != want[i].id || row.AccountCount != want[i].account ||
			row.PendingCount != want[i].pending || row.ReviewCount != want[i].review {
			t.Fatalf("%s row %d = {id=%s account=%d pending=%d review=%d}, want {id=%s account=%d pending=%d review=%d}",
				label, i, row.ID, row.AccountCount, row.PendingCount, row.ReviewCount,
				want[i].id, want[i].account, want[i].pending, want[i].review)
		}
	}
}

// TestUserListPagesThenAggregatesAccounts 钉住分页、分组与计数在改造后不变：
// 页内顺序、每行三计数、三个分组结果、四计数都与 P2-8 基线一致。
func TestUserListPagesThenAggregatesAccounts(t *testing.T) {
	e := newEnv(t)
	f := seedUserListFixture(t, e)

	first, next, err := e.users.List(e.ctx, "", "", "", "", "", 2, false)
	mustNotError(t, err, "list page 1")
	assertUserRows(t, "page 1", first, []wantUserRow{
		{f.master.ID, 1, 0, 0},
		{f.pending.ID, 2, 1, 2},
	})
	if next == "" {
		t.Fatal("page 1 has no next cursor")
	}

	second, next2, err := e.users.List(e.ctx, "", "", "", "", next, 2, false)
	mustNotError(t, err, "list page 2")
	assertUserRows(t, "page 2", second, []wantUserRow{
		{f.normal.ID, 1, 0, 0},
		{f.deletedAcct.ID, 1, 0, 0},
	})
	if next2 == "" {
		t.Fatal("page 2 has no next cursor")
	}

	third, last, err := e.users.List(e.ctx, "", "", "", "", next2, 2, false)
	mustNotError(t, err, "list page 3")
	assertUserRows(t, "page 3", third, []wantUserRow{{f.vacant.ID, 0, 0, 0}})
	if last != "" {
		t.Fatalf("last page returned a next cursor %q", last)
	}

	// 三页并集无重无漏；软删用户不在列表且不能被游标带出。
	seen := map[string]int{}
	for _, page := range [][]coreuser.Record{first, second, third} {
		for _, row := range page {
			seen[row.ID]++
		}
	}
	if len(seen) != 5 {
		t.Fatalf("paged users = %d, want 5: %v", len(seen), seen)
	}
	for id, n := range seen {
		if n != 1 {
			t.Fatalf("user %s appeared %d times across pages", id, n)
		}
	}
	if _, ok := seen[f.deleted.ID]; ok {
		t.Fatal("soft-deleted user appeared in the live list")
	}

	for _, tc := range []struct {
		group coreuser.Group
		want  []wantUserRow
	}{
		{coreuser.GroupPending, []wantUserRow{{f.pending.ID, 2, 1, 2}}},
		{coreuser.GroupNormal, []wantUserRow{
			{f.master.ID, 1, 0, 0},
			{f.normal.ID, 1, 0, 0},
			{f.deletedAcct.ID, 1, 0, 0},
		}},
		{coreuser.GroupVacant, []wantUserRow{{f.vacant.ID, 0, 0, 0}}},
	} {
		rows, cursor, err := e.users.List(e.ctx, "", "", "", tc.group, "", 50, false)
		mustNotError(t, err, "list group "+string(tc.group))
		if cursor != "" {
			t.Fatalf("group %s with 50 rows returned a cursor %q", tc.group, cursor)
		}
		assertUserRows(t, "group "+string(tc.group), rows, tc.want)
	}

	counts, err := e.users.Counts(e.ctx)
	mustNotError(t, err, "counts")
	if counts != (coreuser.Counts{Pending: 1, Normal: 3, Vacant: 1, Deleted: 1}) {
		t.Fatalf("counts = %+v, want {1 3 1 1}", counts)
	}

	// 分组与 include_deleted 互斥：列表层必须报错，不是静默忽略。
	if _, _, err := e.users.List(e.ctx, "", "", "", coreuser.GroupPending, "", 10, true); err == nil {
		t.Fatal("group + include_deleted was accepted")
	}
}

// TestUserListEmptyPageSkipsAccountAggregation 用 tripwire view 钉住
// 「空页不触达账号聚合」：把 accounts 换成任何读取都会 RAISE 的 view 后，
// 不命中的列表仍必须返回空页；同一份 view 下命中用户的列表必须报错，
// 证明 tripwire 真的会响（否则本用例会静默通过）。
func TestUserListEmptyPageSkipsAccountAggregation(t *testing.T) {
	e := newEnv(t)
	owner := e.createUser("tripwire@example.com")
	school := e.createSchool("tripwire")
	e.createAccount(school.ID, owner.ID, "13800000777")

	e.exec("ALTER TABLE accounts RENAME TO accounts_shadow")
	e.exec(`CREATE FUNCTION accounts_tripwire() RETURNS SETOF accounts_shadow
		LANGUAGE plpgsql AS $fn$
		BEGIN
			RAISE EXCEPTION 'accounts aggregation touched';
		END
		$fn$`)
	e.exec("CREATE VIEW accounts AS SELECT * FROM accounts_tripwire()")

	items, next, err := e.users.List(e.ctx, "", "", "no-such-prefix", "", "", 10, false)
	mustNotError(t, err, "empty page must not touch accounts")
	if len(items) != 0 || next != "" {
		t.Fatalf("empty page = (%d rows, next=%q), want (0, \"\")", len(items), next)
	}

	// 正对照：命中用户时列表必须读 accounts，tripwire 必须报错。
	_, _, err = e.users.List(e.ctx, "", "", "", "", "", 10, false)
	if err == nil {
		t.Fatal("non-empty list did not touch accounts; tripwire is not armed")
	}
	if !strings.Contains(err.Error(), "accounts aggregation touched") {
		t.Fatalf("non-empty list error = %v; want the tripwire exception", err)
	}
}
