package pg_test

// P2-9：00033 全局账号列表索引的真库 e2e。
//
// # 口径
//   - 文件保持 NO TRANSACTION + lock_timeout/statement_timeout 的 0 窗口，无 Down；
//   - 迁移在空 schema 上连跑两次都绿，版本停在嵌入版本（本批到 33）；
//   - idx_accounts_created_live 存在且 indisvalid；同一 CIC 语句可重复执行；
//   - 全局账号列表（master、无 school 过滤、created_at/id 倒序）自然计划命中索引。
//
// # 归属
// 测试载体。被测对象是 migrations/postgres/00033 与 pg 的账号读面。

import (
	"os"
	"strings"
	"testing"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
)

// accountsCreatedLiveIndex 是 00033 新增的索引名。
const accountsCreatedLiveIndex = "idx_accounts_created_live"

// TestMigration00033StaticShape 是静态守卫：CIC 必须在 lock_timeout=0 与
// statement_timeout=0 的窗口内，结束后恢复语句超时与 5s 锁超时；不写 Down 段；
// 索引列与局部谓词必须与列表排序/未软删口径一一对应。
func TestMigration00033StaticShape(t *testing.T) {
	body, err := readMigration("00033_accounts_created_index.sql")
	if err != nil {
		t.Fatalf("read 00033: %v", err)
	}
	sql := string(body)
	if !strings.HasPrefix(sql, "-- +goose NO TRANSACTION") {
		t.Fatal("00033 is not a NO TRANSACTION migration; CREATE INDEX CONCURRENTLY would fail")
	}
	if !strings.Contains(sql, irreversibleMarker) {
		t.Fatalf("00033 lacks the forward-only marker %q", irreversibleMarker)
	}
	if strings.Contains(sql, "-- +goose Down") {
		t.Fatal("00033 must not have a Down section; migrations are forward-only")
	}
	zero := strings.Index(sql, "SET lock_timeout='0'")
	five := strings.Index(sql, "SET lock_timeout='5s'")
	stmtZero := strings.Index(sql, "SET statement_timeout='0'")
	stmtReset := strings.Index(sql, "RESET statement_timeout")
	cic := strings.Index(sql, "CREATE INDEX CONCURRENTLY IF NOT EXISTS "+accountsCreatedLiveIndex)
	if zero < 0 || five < 0 || stmtZero < 0 || stmtReset < 0 || cic < 0 {
		t.Fatalf("00033 shape = (lock 0 %d, stmt 0 %d, CIC %d, stmt reset %d, lock 5s %d); want all present",
			zero, stmtZero, cic, stmtReset, five)
	}
	if !(zero < cic && stmtZero < cic && cic < stmtReset && cic < five) {
		t.Fatalf("00033 timeout window is wrong: lock0 %d, stmt0 %d, CIC %d, reset %d, lock5s %d",
			zero, stmtZero, cic, stmtReset, five)
	}
	if !strings.Contains(sql, "ON accounts (created_at DESC, id DESC) WHERE deleted_at IS NULL") {
		t.Fatal("00033 index columns or partial predicate drifted from the global list shape")
	}
}

// TestMigration00033AccountsCreatedIndex 钉住迁移契约：幂等两次、索引有效、
// CIC 语句可原样重跑。
func TestMigration00033AccountsCreatedIndex(t *testing.T) {
	m := newMigrationDownEnv(t)
	embedded, err := pg.EmbeddedVersion()
	mustNotError(t, err, "embedded version")
	if embedded < 33 {
		t.Fatalf("embedded migration version = %d, want >= 33 (00033 not embedded)", embedded)
	}

	first, err := pg.Migrate(m.ctx, m.dsn)
	mustNotError(t, err, "first migrate")
	second, err := pg.Migrate(m.ctx, m.dsn)
	mustNotError(t, err, "second migrate (idempotency)")
	if first != embedded || second != embedded {
		t.Fatalf("migrate versions = (%d, %d), want (%d, %d)", first, second, embedded, embedded)
	}
	if got := m.scalarInt(
		"SELECT max(version_id) FROM goose_db_version WHERE is_applied"); got != embedded {
		t.Fatalf("goose applied version = %d, want %d", got, embedded)
	}

	if !indexIsValid(t, m, accountsCreatedLiveIndex) {
		t.Fatalf("%s is missing or INVALID after 00033", accountsCreatedLiveIndex)
	}

	// 迁移文件里的 CIC 语句原样再跑一次：IF NOT EXISTS 必须幂等且不破坏索引。
	m.exec("CREATE INDEX CONCURRENTLY IF NOT EXISTS " + accountsCreatedLiveIndex +
		" ON accounts (created_at DESC, id DESC) WHERE deleted_at IS NULL")
	if !indexIsValid(t, m, accountsCreatedLiveIndex) {
		t.Fatalf("%s became INVALID after a repeated CIC", accountsCreatedLiveIndex)
	}
}

// TestMigration00033GlobalAccountListPlan 钉住查询计划：全局账号列表
// （master scope、无 school 过滤、created_at/id 倒序 + LIMIT）命中新索引。
func TestMigration00033GlobalAccountListPlan(t *testing.T) {
	m := newMigrationDownEnv(t)
	_, err := pg.Migrate(m.ctx, m.dsn)
	mustNotError(t, err, "migrate")
	seedAccountsCreatedRows(t, m)

	// 与 AccountRepo.List 在 master、无 school 过滤时的访问路径一致：
	// 选择列把 id 输出成 text，排序必须限定 accounts.id；否则裸 id 会解析到
	// 输出列，索引第二列不参与排序，计划退化为 Incremental Sort。
	plan := explainPlain(t, m,
		"SELECT id::text FROM accounts WHERE deleted_at IS NULL "+
			"ORDER BY accounts.created_at DESC, accounts.id DESC LIMIT 51")
	if strings.Contains(plan, "Seq Scan on accounts") {
		t.Fatalf("global account list seq-scans accounts; want %s:\n%s", accountsCreatedLiveIndex, plan)
	}
	if strings.Contains(plan, "Sort") {
		t.Fatalf("global account list still sorts; %s should provide the order:\n%s", accountsCreatedLiveIndex, plan)
	}
	if !strings.Contains(plan, accountsCreatedLiveIndex) {
		t.Fatalf("global account list plan misses %s:\n%s", accountsCreatedLiveIndex, plan)
	}
	t.Logf("global account list plan:\n%s", plan)
}

// TestMigration00033IndexRecovery 钉住恢复文档（deploy/README.md §7.1）。
// 场景：并发建索引被取消，留下同名 INVALID 索引，且 goose 已记录版本 33。
// 断言三件事：重跑 Migrate 不会修 INVALID；健康索引不受影响；按 README
// 的“只重建 INVALID 那条”手工路径能恢复 indisvalid=true 并被计划选中。
func TestMigration00033IndexRecovery(t *testing.T) {
	m := newMigrationDownEnv(t)
	if _, err := pg.Migrate(m.ctx, m.dsn); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	seedAccountsCreatedRows(t, m)

	// 用真实失败制造 INVALID：并发唯一索引在重复的 created_at 上失败，
	// PostgreSQL 会留下 indisvalid=false 的半成品索引。
	m.exec("DROP INDEX " + accountsCreatedLiveIndex)
	m.exec("UPDATE accounts SET created_at = TIMESTAMPTZ '2020-01-01 00:00:00+00' " +
		"WHERE id IN (SELECT id FROM accounts LIMIT 2)")
	if err := m.tryExec("CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS " +
		accountsCreatedLiveIndex + " ON accounts (created_at) WHERE deleted_at IS NULL"); err == nil {
		t.Fatalf("unique index build unexpectedly succeeded; need duplicate created_at rows")
	}
	if indexIsValid(t, m, accountsCreatedLiveIndex) {
		t.Fatalf("%s is valid after failed concurrent build; want INVALID", accountsCreatedLiveIndex)
	}

	// goose 已记录最新版本：重跑不会再执行 00033；IF NOT EXISTS 也不修 INVALID。
	embedded, err := pg.EmbeddedVersion()
	mustNotError(t, err, "embedded version")
	version, err := pg.Migrate(m.ctx, m.dsn)
	if err != nil {
		t.Fatalf("re-migrate: %v", err)
	}
	if version != embedded {
		t.Fatalf("re-migrate version = %d, want %d", version, embedded)
	}
	if indexIsValid(t, m, accountsCreatedLiveIndex) {
		t.Fatalf("re-running Migrate repaired %s; recovery would be unnecessary", accountsCreatedLiveIndex)
	}

	// README 恢复：只 DROP INVALID 那条，按 00033 原样重建，再 ANALYZE。
	m.exec("DROP INDEX CONCURRENTLY IF EXISTS " + accountsCreatedLiveIndex)
	m.exec("CREATE INDEX CONCURRENTLY IF NOT EXISTS " + accountsCreatedLiveIndex +
		" ON accounts (created_at DESC, id DESC) WHERE deleted_at IS NULL")
	m.exec("ANALYZE accounts")
	if !indexIsValid(t, m, accountsCreatedLiveIndex) {
		t.Fatalf("manual rebuild left %s INVALID", accountsCreatedLiveIndex)
	}

	plan := explainPlain(t, m,
		"SELECT id::text FROM accounts WHERE deleted_at IS NULL "+
			"ORDER BY accounts.created_at DESC, accounts.id DESC LIMIT 51")
	if strings.Contains(plan, "Seq Scan on accounts") || strings.Contains(plan, "Sort") ||
		!strings.Contains(plan, accountsCreatedLiveIndex) {
		t.Fatalf("recovered index is not used by the global list plan:\n%s", plan)
	}
}

// readMigration 读迁移文件正文。其它迁移 e2e 用同样相对路径定位。
func readMigration(name string) ([]byte, error) {
	return os.ReadFile("../../../migrations/postgres/" + name)
}

// seedAccountsCreatedRows 写入足够让规划器选择索引的账号行并刷新统计。
func seedAccountsCreatedRows(t *testing.T, m *migrationDownEnv) {
	t.Helper()
	m.exec("INSERT INTO users (email, password_hash) VALUES ('mig33@example.com', 'argon2id$test')")
	m.exec("INSERT INTO schools (code, name) VALUES ('mig33', 'Migration 33')")
	m.exec(`INSERT INTO accounts (school_id, owner_user_id, phone, created_at)
		SELECT s.id, u.id, '13' || lpad(g::text, 9, '0'),
		       TIMESTAMPTZ '2026-09-01 00:00:00+00' + (g * interval '1 minute')
		  FROM schools s, users u, generate_series(1, 20000) g`)
	if got := m.scalarInt("SELECT count(*) FROM accounts"); got != 20000 {
		t.Fatalf("seeded accounts rows = %d, want 20000", got)
	}
	m.exec("ANALYZE accounts")
}
