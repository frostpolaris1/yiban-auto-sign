package pg_test

// accounts 仓储 e2e：列表排序与游标续读。
// 依赖真实 PostgreSQL：排序键、键集比较与 uuid 排序都在库侧执行。

import (
	"context"
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

// TestAccountCredentialsBatch 钉住批量凭据读取的筛选口径。
// 判据：多 ID 一次读回、缺少凭据的行缺席、软删行与作用域外行不出现、
// 空 ids 不发查询直接回空 map。
func TestAccountCredentialsBatch(t *testing.T) {
	e := newEnv(t)
	schoolA := e.createSchool("cred-batch-a")
	schoolB := e.createSchool("cred-batch-b")
	ownerA := e.createUser("cred-batch-a@example.com")
	ownerB := e.createUser("cred-batch-b@example.com")

	first := e.createAccount(schoolA.ID, ownerA.ID, "13800000020")
	second := e.createAccount(schoolA.ID, ownerA.ID, "13800000021")
	other := e.createAccount(schoolB.ID, ownerB.ID, "13800000022")
	noCredential := e.createAccount(schoolA.ID, ownerA.ID, "13800000023")

	insert := func(rec coreaccount.Record, kid string) {
		t.Helper()
		cred := coreaccount.Credential{
			AccountID:  rec.ID,
			SchoolID:   rec.SchoolID,
			Alg:        "AES-256-GCM",
			KID:        kid,
			AADVersion: 1,
			Nonce:      []byte("123456789012"),
			Ciphertext: []byte("0123456789abcdef"),
			Version:    1,
		}
		mustNotError(t, e.db.InTx(e.ctx, func(ctx context.Context) error {
			return e.accts.InsertCredential(ctx, cred)
		}), "insert credential for "+rec.ID)
	}
	insert(first, "0000000000000001")
	insert(second, "0000000000000002")
	insert(other, "0000000000000003")

	ids := []string{first.ID, second.ID, other.ID, noCredential.ID}
	got, err := e.accts.Credentials(e.ctx, corescope.Master(), ids)
	mustNotError(t, err, "batch read credentials")
	if len(got) != 3 {
		t.Fatalf("batch returned %d credentials, want 3", len(got))
	}
	if got[first.ID].KID != "0000000000000001" || got[other.ID].KID != "0000000000000003" {
		t.Fatalf("batch keyed the wrong rows: %+v", got)
	}
	if _, ok := got[noCredential.ID]; ok {
		t.Fatal("a row without a credential must stay absent")
	}

	// 空 ids 直接回空 map。
	empty, err := e.accts.Credentials(e.ctx, corescope.Master(), nil)
	mustNotError(t, err, "empty batch")
	if len(empty) != 0 {
		t.Fatalf("empty batch returned %d rows, want 0", len(empty))
	}

	// user 作用域只读自己的行。
	users, err := e.accts.Credentials(e.ctx, corescope.User(ownerA.ID), ids)
	mustNotError(t, err, "user scoped batch")
	if len(users) != 2 {
		t.Fatalf("user scoped batch returned %d rows, want 2", len(users))
	}
	if _, ok := users[other.ID]; ok {
		t.Fatal("a user batch leaked another owner's credential")
	}

	// admin 作用域只读绑定院校的行。
	admins, err := e.accts.Credentials(e.ctx, corescope.Admin([]string{schoolB.ID}), ids)
	mustNotError(t, err, "admin scoped batch")
	if len(admins) != 1 {
		t.Fatalf("admin scoped batch returned %d rows, want 1", len(admins))
	}
	if _, ok := admins[other.ID]; !ok {
		t.Fatal("admin scoped batch dropped the bound-school credential")
	}

	// 软删行的凭据不再读出。
	mustNotError(t, e.accts.SoftDelete(e.ctx, corescope.Master(), first.ID, ownerA.ID,
		time.Date(2026, 11, 3, 0, 0, 0, 0, time.UTC)), "soft delete an account")
	after, err := e.accts.Credentials(e.ctx, corescope.Master(), ids)
	mustNotError(t, err, "batch after soft delete")
	if _, ok := after[first.ID]; ok {
		t.Fatal("a soft-deleted account's credential was returned")
	}
	if len(after) != 2 {
		t.Fatalf("batch after soft delete returned %d rows, want 2", len(after))
	}
}
