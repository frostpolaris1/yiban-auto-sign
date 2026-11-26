package pg_test

// 窗口发现候选 e2e：候选排序、过滤与 limit。
// 依赖真实 PostgreSQL：复合排序键、id 兜底与过滤都在库侧执行。

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
// 排序刻意不读 last_verified_at；本辅助只用来证明「验证时间再新也
// 不能把候选提前」。其余字段用固定哑值满足库侧形状约束。
func (e *testEnv) seedDiscoveryCredential(accountID, schoolID string, verifiedAt *time.Time) {
	e.t.Helper()
	e.exec(`INSERT INTO credentials (account_id, school_id, kid, nonce, ciphertext, last_verified_at)
		VALUES ($1::uuid, $2::uuid, '0011223344556677',
			decode('000102030405060708090a0b', 'hex'),
			decode(repeat('ab', 16), 'hex'), $3)`,
		accountID, schoolID, verifiedAt)
}

// TestDiscoveryCandidatesOrdersOldestFirst 钉住候选主排序键是 created_at ASC。
//
// 判据一：更早建立的账号先探，与 credentials.last_verified_at 无关。
// 判据二：created_at 最新但从未验证的新注册号排最后——它正是会把全校
// 窗口发现带进连续失败的坏号；若排序退化成 created_at DESC，本用例红。
// 判据三：坏号即使带最新 last_verified_at 也不能排到首位，防止没有写入方
// 的列重新变成虚假保证。
func TestDiscoveryCandidatesOrdersOldestFirst(t *testing.T) {
	e := newEnv(t)
	school := e.createSchool("disc-order")
	owner := e.createUser("disc-order@example.com")
	base := time.Date(2026, 10, 29, 0, 0, 0, 0, time.UTC)
	oldVerified := base.Add(-96 * time.Hour)
	newVerified := base.Add(12 * time.Hour)

	// 新注册坏号：created_at 最新，却带着最新的验证时间。
	badID := discoveryIDHigh
	e.seedDiscoveryAccount(badID, school.ID, owner.ID, "13900000001", base.Add(24*time.Hour))
	e.seedDiscoveryCredential(badID, school.ID, &newVerified)

	// 最老的好号：没有验证记录。
	staleID := discoveryIDMid
	e.seedDiscoveryAccount(staleID, school.ID, owner.ID, "13900000002", base.Add(-48*time.Hour))

	// 中间号：验证时间比坏号旧。
	freshID := discoveryIDLow
	e.seedDiscoveryAccount(freshID, school.ID, owner.ID, "13900000003", base.Add(-24*time.Hour))
	e.seedDiscoveryCredential(freshID, school.ID, &oldVerified)

	got, err := e.accts.DiscoveryCandidatesForSchool(e.ctx, school.ID, 10)
	mustNotError(t, err, "discovery candidates")
	want := []string{staleID, freshID, badID}
	if len(got) != len(want) {
		t.Fatalf("got %d candidates, want %d", len(got), len(want))
	}
	for i := range want {
		if got[i].ID != want[i] {
			t.Fatalf("candidate %d is %s; want %s (order: created_at ASC, id ASC)",
				i, got[i].ID, want[i])
		}
	}
	if got[len(got)-1].ID != badID {
		t.Fatalf("newest never-verified account %s must sort last; got %v", badID, ids(got))
	}
}

// TestDiscoveryCandidatesTieBreaksAndFilters 钉住并列裁决与过滤口径。
//
// 判据一：created_at 相同时按 id 升序，顺序确定。
// 判据二：软删行、非 active 行（paused_admin）与 pending 行都不出现。
// 判据三：limit 生效且非法 limit 返回参数错误，不静默置 0。
func TestDiscoveryCandidatesTieBreaksAndFilters(t *testing.T) {
	e := newEnv(t)
	school := e.createSchool("disc-tie")
	owner := e.createUser("disc-tie@example.com")
	base := time.Date(2026, 10, 28, 0, 0, 0, 0, time.UTC)

	// created_at 最早的先；同一时刻按 id 升序。
	equalCreated := []struct {
		id        string
		createdAt time.Time
		phone     string
	}{
		{discoveryIDHigh, base, "13900000011"},               // created_at 最早
		{discoveryIDLow, base.Add(time.Hour), "13900000012"}, // 同 created_at 组内 id 最小
		{discoveryIDMid, base.Add(time.Hour), "13900000013"}, // 同 created_at 组内 id 次小
	}
	for _, seed := range equalCreated {
		e.seedDiscoveryAccount(seed.id, school.ID, owner.ID, seed.phone, seed.createdAt)
	}

	// 最晚建号：排在最后。
	unverifiedID := "00000000-0000-4000-8000-0000000000ff"
	e.seedDiscoveryAccount(unverifiedID, school.ID, owner.ID, "13900000014", base.Add(48*time.Hour))

	// 软删的 active 行不得入选，即使带凭据与验证时间。
	deletedID := "00000000-0000-4000-8000-0000000000aa"
	e.seedDiscoveryAccount(deletedID, school.ID, owner.ID, "13900000015", base.Add(-time.Hour))
	newest := base.Add(96 * time.Hour)
	e.seedDiscoveryCredential(deletedID, school.ID, &newest)
	e.exec("UPDATE accounts SET deleted_at = now() WHERE id = $1::uuid", deletedID)

	// 非 active：paused_admin。
	pausedID := "00000000-0000-4000-8000-0000000000bb"
	e.seedDiscoveryAccount(pausedID, school.ID, owner.ID, "13900000016", base.Add(-time.Hour))
	e.exec("UPDATE accounts SET status = 'paused_admin' WHERE id = $1::uuid", pausedID)

	// pending：未审核通过。
	pendingID := "00000000-0000-4000-8000-0000000000cc"
	e.exec(`INSERT INTO accounts (id, school_id, owner_user_id, phone, created_at)
		VALUES ($1::uuid, $2::uuid, $3::uuid, '13900000017', $4)`,
		pendingID, school.ID, owner.ID, base.Add(-time.Hour))

	got, err := e.accts.DiscoveryCandidatesForSchool(e.ctx, school.ID, 10)
	mustNotError(t, err, "discovery candidates")
	want := []string{discoveryIDHigh, discoveryIDLow, discoveryIDMid, unverifiedID}
	if len(got) != len(want) {
		t.Fatalf("got %d candidates %v; want %d", len(got), ids(got), len(want))
	}
	for i := range want {
		if got[i].ID != want[i] {
			t.Fatalf("candidate %d is %s; want %s (created_at ASC, id ASC)",
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
