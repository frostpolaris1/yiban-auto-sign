package pg_test

// accounts 仓储 e2e：列表排序与游标续读。
// 依赖真实 PostgreSQL：排序键、键集比较与 uuid 排序都在库侧执行。

import (
	"sort"
	"testing"
	"time"

	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// TestAccountListOrdersAndPaginates 钉住账号列表的排序键与游标续读。
//
// 判据一：较新的行一定排在前；同 created_at 的行按 id 倒序，顺序确定。
// 判据二：游标续页不重不漏，三页并集等于全集。
// 判据三：游标来自本页末行，下一行严格小于游标键。
//
// 删掉 ORDER BY 时 PostgreSQL 按堆序返回，同组行变成 id 升序——本用例红。
// 只按 created_at 排序时同组行顺序不定——本用例红。
func TestAccountListOrdersAndPaginates(t *testing.T) {
	e := newEnv(t)
	school := e.createSchool("acct-order")
	owner := e.createUser("acct-order@example.com")
	newer := time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)
	older := newer.Add(-48 * time.Hour)

	type seeded struct {
		id string
		at time.Time
	}
	var all []seeded
	add := func(phone string, at time.Time) {
		rec := e.createAccount(school.ID, owner.ID, phone)
		if _, err := e.db.Exec(e.ctx,
			"UPDATE accounts SET created_at = $2 WHERE id = $1::uuid", rec.ID, at); err != nil {
			t.Fatalf("pin created_at for %s: %v", rec.ID, err)
		}
		all = append(all, seeded{id: rec.ID, at: at})
	}
	for _, phone := range []string{
		"13800000010", "13800000011", "13800000012", "13800000013",
	} {
		add(phone, newer)
	}
	for _, phone := range []string{"13800000020", "13800000021"} {
		add(phone, older)
	}
	if len(all) != 6 {
		t.Fatalf("seeded %d accounts, want 6", len(all))
	}

	// 期望顺序＝(created_at, id) 倒序。uuid 文本序与 PostgreSQL uuid 字节序一致。
	sort.Slice(all, func(i, j int) bool {
		if all[i].at.Equal(all[j].at) {
			return all[i].id > all[j].id
		}
		return all[i].at.After(all[j].at)
	})

	var got []coreaccount.Record
	cursor := ""
	for page := 1; ; page++ {
		items, next, err := e.accts.List(e.ctx, corescope.Master(), "", "", "", cursor, 2, false)
		if err != nil {
			t.Fatalf("list page %d: %v", page, err)
		}
		if len(items) > 2 {
			t.Fatalf("page %d returned %d rows for limit 2", page, len(items))
		}
		for i := 0; i+1 < len(items); i++ {
			left, right := items[i], items[i+1]
			if left.CreatedAt.Equal(right.CreatedAt) {
				if left.ID <= right.ID {
					t.Fatalf("page %d breaks id DESC for equal created_at: %s then %s",
						page, left.ID, right.ID)
				}
			} else if left.CreatedAt.Before(right.CreatedAt) {
				t.Fatalf("page %d breaks created_at DESC: %s then %s",
					page, left.CreatedAt, right.CreatedAt)
			}
		}
		got = append(got, items...)
		if next == "" {
			break
		}
		if page == 1 && len(items) != 2 {
			t.Fatalf("first page returned %d rows; want 2 before a cursor", len(items))
		}
		cursor = next
		if page > 10 {
			t.Fatal("cursor pagination did not terminate")
		}
	}

	if len(got) != len(all) {
		t.Fatalf("pagination returned %d rows, want %d", len(got), len(all))
	}
	for i := range all {
		if got[i].ID != all[i].id {
			t.Fatalf("row %d is %s; want %s (pagination reordered or repeated)", i, got[i].ID, all[i].id)
		}
		if !got[i].CreatedAt.UTC().Equal(all[i].at) {
			t.Fatalf("row %d created_at is %s; want %s", i, got[i].CreatedAt, all[i].at)
		}
	}
}
