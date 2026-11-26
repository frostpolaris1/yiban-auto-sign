package pg_test

// 窗口发现候选 e2e：候选排序、过滤与 limit。
// 依赖真实 PostgreSQL：NULLS LAST、复合排序键与凭据聚合都在库侧执行。

import (
	"testing"
	"time"

	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

// 固定 uuid 让「同键时按 id 升序」的裁决可断言。
// uuid 文本序与 PostgreSQL 的字节序一致。
const (
	discoveryIDLow  = "00000000-0000-4000-8000-000000000001"
	discoveryIDMid  = "00000000-0000-4000-8000-000000000002"
	discoveryIDHigh = "00000000-0000-4000-8000-00000000000f"
)

// seedDiscoveryAccount 直接写一行账号并审核为 active。
//
// 候选排序要控 id 与 created_at，而 createAccount 只接受属主与手机号、
// 两个键都由库决定，故这里显式指定。
func (e *testEnv) seedDiscoveryAccount(id, schoolID, ownerID, phone string, createdAt time.Time) {
	e.t.Helper()
	e.exec(`INSERT INTO accounts (id, school_id, owner_user_id, phone, created_at)
		VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5)`,
		id, schoolID, ownerID, phone, createdAt)
	e.activateAccount(id)
}

// seedDiscoveryCredential 写一行已加密凭据。
//
// 本用例只在意 last_verified_at；其余字段用固定哑值满足库侧形状约束
// （kid 16 位十六进制、nonce 12 字节、ciphertext 至少 16 字节）。
func (e *testEnv) seedDiscoveryCredential(accountID, schoolID string, verifiedAt *time.Time) {
	e.t.Helper()
	e.exec(`INSERT INTO credentials (account_id, school_id, kid, nonce, ciphertext, last_verified_at)
		VALUES ($1::uuid, $2::uuid, '0011223344556677',
			decode('000102030405060708090a0b', 'hex'),
			decode(repeat('ab', 16), 'hex'), $3)`,
		accountID, schoolID, verifiedAt)
}

// TestDiscoveryCandidatesOrdersVerifiedBeforeNewBadAccount 钉住候选主排序键。
//
// 判据一：last_verified_at 较新者在前，已验证的三行按时间倒序。
// 判据二：从未验证（last_verified_at 为 NULL）的行排在任何已验证行之后
// （NULLS LAST）。
// 判据三：created_at 最新但从未验证的新注册号排最后——它正是会把全校
// 窗口发现带进连续失败的坏号；若排序退化成 created_at DESC，本用例红。
func TestDiscoveryCandidatesOrdersVerifiedBeforeNewBadAccount(t *testing.T) {
	e := newEnv(t)
	school := e.createSchool("disc-order")
	owner := e.createUser("disc-order@example.com")
	base := time.Date(2026, 10, 29, 0, 0, 0, 0, time.UTC)
	verifiedNew := base.Add(12 * time.Hour)
	verifiedOld := base.Add(-96 * time.Hour)

	// 新注册坏号：created_at 最新，但从未验证成功。
	badID := discoveryIDHigh
	e.seedDiscoveryAccount(badID, school.ID, owner.ID, "13900000001", base.Add(24*time.Hour))

	staleID := discoveryIDMid
	e.seedDiscoveryAccount(staleID, school.ID, owner.ID, "13900000002", base.Add(-48*time.Hour))
	e.seedDiscoveryCredential(staleID, school.ID, &verifiedOld)

	freshID := discoveryIDLow
	e.seedDiscoveryAccount(freshID, school.ID, owner.ID, "13900000003", base.Add(-24*time.Hour))
	e.seedDiscoveryCredential(freshID, school.ID, &verifiedNew)

	got, err := e.accts.DiscoveryCandidatesForSchool(e.ctx, school.ID, 10)
	mustNotError(t, err, "discovery candidates")
	want := []string{freshID, staleID, badID}
	if len(got) != len(want) {
		t.Fatalf("got %d candidates, want %d", len(got), len(want))
	}
	for i := range want {
		if got[i].ID != want[i] {
			t.Fatalf("candidate %d is %s; want %s (order: last_verified_at DESC NULLS LAST)", i, got[i].ID, want[i])
		}
	}
	if got[len(got)-1].ID != badID {
		t.Fatalf("newest never-verified account %s must sort last; got %v", badID, ids(got))
	}
}

// TestDiscoveryCandidatesTieBreaksAndFilters 钉住并列裁决与过滤口径。
//
// 判据一：last_verified_at 相同时按 created_at 升序（更早建号先探）。
// 判据二：last_verified_at 与 created_at 都相同时按 id 升序，顺序确定。
// 判据三：从未验证的行仍排最后。
// 判据四：软删行、非 active 行（paused_admin）与 pending 行都不出现。
// 判据五：limit 生效且非法 limit 返回参数错误，不静默置 0。
func TestDiscoveryCandidatesTieBreaksAndFilters(t *testing.T) {
	e := newEnv(t)
	school := e.createSchool("disc-tie")
	owner := e.createUser("disc-tie@example.com")
	base := time.Date(2026, 10, 28, 0, 0, 0, 0, time.UTC)
	verified := base.Add(6 * time.Hour)

	// 同 last_verified_at：created_at 更早者先，其次按 id 升序。
	equalVerified := []struct {
		id        string
		createdAt time.Time
		phone     string
	}{
		{discoveryIDHigh, base, "13900000011"},               // created_at 最早
		{discoveryIDLow, base.Add(time.Hour), "13900000012"}, // 同 created_at 组内 id 最小
		{discoveryIDMid, base.Add(time.Hour), "13900000013"}, // 同 created_at 组内 id 次小
	}
	for _, seed := range equalVerified {
		e.seedDiscoveryAccount(seed.id, school.ID, owner.ID, seed.phone, seed.createdAt)
		at := verified
		e.seedDiscoveryCredential(seed.id, school.ID, &at)
	}

	// 从未验证：created_at 最新也排最后。
	unverifiedID := "00000000-0000-4000-8000-0000000000ff"
	e.seedDiscoveryAccount(unverifiedID, school.ID, owner.ID, "13900000014", base.Add(48*time.Hour))

	// 软删的 active 行：验证时间最新也不得入选。
	deletedID := "00000000-0000-4000-8000-0000000000aa"
	e.seedDiscoveryAccount(deletedID, school.ID, owner.ID, "13900000015", base.Add(-time.Hour))
	newest := base.Add(96 * time.Hour)
	e.seedDiscoveryCredential(deletedID, school.ID, &newest)
	e.exec("UPDATE accounts SET deleted_at = now() WHERE id = $1::uuid", deletedID)

	// 非 active：paused_admin，同样带最新验证时间。
	pausedID := "00000000-0000-4000-8000-0000000000bb"
	e.seedDiscoveryAccount(pausedID, school.ID, owner.ID, "13900000016", base.Add(-time.Hour))
	e.seedDiscoveryCredential(pausedID, school.ID, &newest)
	e.exec("UPDATE accounts SET status = 'paused_admin' WHERE id = $1::uuid", pausedID)

	// pending：未审核通过。
	pendingID := "00000000-0000-4000-8000-0000000000cc"
	e.exec(`INSERT INTO accounts (id, school_id, owner_user_id, phone, created_at)
		VALUES ($1::uuid, $2::uuid, $3::uuid, '13900000017', $4)`,
		pendingID, school.ID, owner.ID, base.Add(-time.Hour))
	e.seedDiscoveryCredential(pendingID, school.ID, &newest)

	got, err := e.accts.DiscoveryCandidatesForSchool(e.ctx, school.ID, 10)
	mustNotError(t, err, "discovery candidates")
	want := []string{
		discoveryIDHigh,
		discoveryIDLow,
		discoveryIDMid,
		unverifiedID,
	}
	if len(got) != len(want) {
		t.Fatalf("got %d candidates %v; want %d", len(got), ids(got), len(want))
	}
	for i := range want {
		if got[i].ID != want[i] {
			t.Fatalf("candidate %d is %s; want %s (created_at ASC, id ASC, NULLS LAST)",
				i, got[i].ID, want[i])
		}
	}
	for _, excluded := range []string{deletedID, pausedID, pendingID} {
		for _, rec := range got {
			if rec.ID == excluded {
				t.Fatalf("candidate list includes excluded account %s", excluded)
			}
		}
	}

	limited, err := e.accts.DiscoveryCandidatesForSchool(e.ctx, school.ID, 2)
	mustNotError(t, err, "limited candidates")
	if len(limited) != 2 {
		t.Fatalf("limit 2 returned %d rows", len(limited))
	}
	for i := range limited {
		if limited[i].ID != want[i] {
			t.Fatalf("limited candidate %d is %s; want %s", i, limited[i].ID, want[i])
		}
	}

	for _, bad := range []int{0, -1, 11} {
		_, err := e.accts.DiscoveryCandidatesForSchool(e.ctx, school.ID, bad)
		if code := codeOf(t, err); code != coreerrs.CodeValidation {
			t.Fatalf("limit %d returned %q; want %q", bad, code, coreerrs.CodeValidation)
		}
	}
}

// ids 取候选 id 序列，供失败信息使用。
func ids(records []coreaccount.Record) []string {
	out := make([]string, 0, len(records))
	for _, rec := range records {
		out = append(out, rec.ID)
	}
	return out
}
