package pg_test

// 00032 sign_tasks day 索引与统计目标的真库 e2e（P0-3）。
//
// 口径：
//   - 迁移在空 schema 上连跑两次都绿，版本停在 32；
//   - 两个索引都 indisvalid（CONCURRENTLY 中断会留 INVALID，必须能查出来）；
//   - sign_tasks 自带两个 autovacuum 统计目标，统计维护不靠手工 ANALYZE；
//   - 计划断言走 EXPLAIN：ListTasks / StateCounts / FailureCounts 三种查询形状
//     在足量行 + ANALYZE 后命中新索引，而不是只查索引存在。
//
// # 归属
// 测试载体。被测对象是 migrations/postgres/00032 与 pg 的按日查询面。

import (
	"strings"
	"testing"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
)

// 00032 新增的两个索引名。
const (
	dayIndexRunAt = "idx_sign_tasks_day_run_at_id"
	dayIndexState = "idx_sign_tasks_day_state"
)

// TestMigration00032DayIndexes 钉住迁移契约：幂等两次、索引有效、统计目标齐全。
func TestMigration00032DayIndexes(t *testing.T) {
	m := newMigrationDownEnv(t)

	first, err := pg.Migrate(m.ctx, m.dsn)
	if err != nil {
		t.Fatalf("first migrate: %v", err)
	}
	second, err := pg.Migrate(m.ctx, m.dsn)
	if err != nil {
		t.Fatalf("second migrate (idempotency): %v", err)
	}
	if first != 32 || second != 32 {
		t.Fatalf("migrate versions = (%d, %d), want (32, 32)", first, second)
	}
	if got := m.scalarInt(
		"SELECT max(version_id) FROM goose_db_version WHERE is_applied"); got != 32 {
		t.Fatalf("goose applied version = %d, want 32", got)
	}

	// 两索引存在且 indisvalid：INVALID 索引仍在 pg_class 里，
	// 只查 to_regclass 会把「中断的半成品」当成绿。
	got := map[string]bool{}
	rows, err := m.db.QueryContext(m.ctx, "SELECT c.relname, i.indisvalid "+
		"FROM pg_index i "+
		"JOIN pg_class c ON c.oid = i.indexrelid "+
		"JOIN pg_namespace n ON n.oid = c.relnamespace "+
		"WHERE n.nspname = current_schema() AND c.relname IN ($1, $2)",
		dayIndexRunAt, dayIndexState)
	if err != nil {
		t.Fatalf("query pg_index: %v", err)
	}
	defer rows.Close()
	for rows.Next() {
		var name string
		var valid bool
		if err := rows.Scan(&name, &valid); err != nil {
			t.Fatalf("scan pg_index: %v", err)
		}
		got[name] = valid
	}
	if err := rows.Err(); err != nil {
		t.Fatalf("iterate pg_index: %v", err)
	}
	for _, name := range []string{dayIndexRunAt, dayIndexState} {
		valid, ok := got[name]
		if !ok {
			t.Fatalf("index %s is missing after 00032", name)
		}
		if !valid {
			t.Fatalf("index %s is INVALID after 00032", name)
		}
	}

	// 自带 autovacuum 统计目标：不只是索引形状，表的统计刷新策略也落库。
	relopts := m.scalarString("SELECT coalesce(array_to_string(reloptions, ','), '') " +
		"FROM pg_class WHERE oid = 'sign_tasks'::regclass")
	for _, want := range []string{
		"autovacuum_analyze_scale_factor=0.01",
		"autovacuum_analyze_threshold=500",
	} {
		if !strings.Contains(relopts, want) {
			t.Fatalf("sign_tasks reloptions = %q; missing %q", relopts, want)
		}
	}
}

// TestMigration00032DayIndexPlans 钉住查询计划：三种按日查询形状都命中新索引。
func TestMigration00032DayIndexPlans(t *testing.T) {
	m := newMigrationDownEnv(t)
	if _, err := pg.Migrate(m.ctx, m.dsn); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	seedDayIndexRows(t, m)

	day := "2026-10-05"

	queries := dayIndexQueries()

	// 强制开关组：把 Seq Scan 与 Sort 关掉，固定验证索引本身可用。
	for _, q := range queries {
		plan := explainWithSeqscanOff(t, m, q.sql, day)
		wantIndexPlan(t, q.label+" (forced)", plan, q.index)
	}

	// 自然计划组：不加任何开关，验证 12000 行 + ANALYZE 后规划器自己选索引。
	// 强制组几乎恒真；这组才证明日常查询会命中新索引。
	for _, q := range queries {
		plan := explainPlain(t, m, q.sql, day)
		wantSignTasksIndexPlan(t, q.label+" (natural)", plan, q.index)
	}
}

// dayIndexQuery 是一种按日查询形状：ListTasks / StateCounts / FailureCounts。
type dayIndexQuery struct {
	label string
	sql   string
	index string
}

// dayIndexQueries 返回三种查询形状：日过滤 + 倒序分页、按日分组、日+状态过滤。
func dayIndexQueries() []dayIndexQuery {
	return []dayIndexQuery{
		{
			// ListTasks 形状：day 过滤 + run_at/id 倒序 + LIMIT。
			label: "ListTasks",
			sql: "SELECT s.id, s.run_at, s.updated_at " +
				"FROM sign_tasks s " +
				"JOIN school_tasks st ON st.id = s.school_task_id " +
				"JOIN schools sc ON sc.id = st.school_id " +
				"JOIN accounts a ON a.id = s.account_id " +
				"WHERE s.day = $1::date " +
				"ORDER BY s.run_at DESC, s.id DESC " +
				"LIMIT 50",
			index: dayIndexRunAt,
		},
		{
			// StateCounts 形状：day 过滤 + GROUP BY state。
			label: "StateCounts",
			sql: "SELECT st.state::text, count(*)::int " +
				"FROM sign_tasks st " +
				"JOIN accounts a ON a.id = st.account_id " +
				"WHERE st.day = $1::date AND a.deleted_at IS NULL " +
				"GROUP BY st.state",
			index: dayIndexState,
		},
		{
			// FailureCounts 形状：day + state 过滤 + GROUP BY result。
			label: "FailureCounts",
			sql: "SELECT t.result, count(*)::int " +
				"FROM sign_tasks t " +
				"JOIN school_tasks st ON st.id = t.school_task_id " +
				"JOIN accounts a ON a.id = t.account_id " +
				"WHERE t.day = $1::date AND t.state = 'failed' AND a.deleted_at IS NULL " +
				"GROUP BY t.result",
			index: dayIndexState,
		},
	}
}

// TestMigration00032IndexRecovery 钉住恢复文档（deploy/README.md §7.1）。
// 场景：并发建索引中断，留下同名 INVALID 索引，且 goose 已记录版本 32。
// 断言三件事：重跑 Migrate 不会修 INVALID；健康索引不受影响；按 README
// 的「只重建 INVALID 那条」手工路径能恢复 indisvalid=true 并重新被计划选中。
func TestMigration00032IndexRecovery(t *testing.T) {
	m := newMigrationDownEnv(t)
	if _, err := pg.Migrate(m.ctx, m.dsn); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	seedDayIndexRows(t, m)

	// 用真实失败制造 INVALID：并发唯一索引在 (day,state) 的重复键上失败，
	// PostgreSQL 会留下 indisvalid=false 的半成品索引。
	m.exec("DROP INDEX " + dayIndexState)
	if err := m.tryExec("CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS " +
		dayIndexState + " ON sign_tasks (day, state)"); err == nil {
		t.Fatalf("unique index build unexpectedly succeeded; need duplicate (day,state) rows")
	}
	if indexIsValid(t, m, dayIndexState) {
		t.Fatalf("%s is valid after failed concurrent build; want INVALID", dayIndexState)
	}
	if !indexIsValid(t, m, dayIndexRunAt) {
		t.Fatalf("healthy %s disappeared during simulation", dayIndexRunAt)
	}

	// goose 已记录 32：重跑只返回 32，不会执行 00032；IF NOT EXISTS 也不修 INVALID。
	version, err := pg.Migrate(m.ctx, m.dsn)
	if err != nil {
		t.Fatalf("re-migrate: %v", err)
	}
	if version != 32 {
		t.Fatalf("re-migrate version = %d, want 32", version)
	}
	if indexIsValid(t, m, dayIndexState) {
		t.Fatalf("re-running Migrate repaired %s; recovery would be unnecessary", dayIndexState)
	}

	// README 恢复 A：只 DROP INVALID 那条，按 00032 原样重建，再 ANALYZE。
	m.exec("DROP INDEX CONCURRENTLY IF EXISTS " + dayIndexState)
	m.exec("CREATE INDEX CONCURRENTLY IF NOT EXISTS " + dayIndexState +
		" ON sign_tasks (day, state)")
	m.exec("ANALYZE sign_tasks")
	if !indexIsValid(t, m, dayIndexState) {
		t.Fatalf("manual rebuild left %s INVALID", dayIndexState)
	}
	if !indexIsValid(t, m, dayIndexRunAt) {
		t.Fatalf("manual rebuild touched healthy %s", dayIndexRunAt)
	}

	// 重建后 StateCounts 形状重新命中 idx_sign_tasks_day_state（自然计划）。
	plan := explainPlain(t, m, dayIndexQueries()[1].sql, "2026-10-05")
	wantSignTasksIndexPlan(t, "StateCounts (recovered)", plan, dayIndexState)
}

// indexIsValid 读 pg_index.indisvalid；索引不存在时直接失败。
func indexIsValid(t *testing.T, m *migrationDownEnv, name string) bool {
	t.Helper()
	var valid bool
	err := m.db.QueryRowContext(m.ctx, "SELECT i.indisvalid "+
		"FROM pg_index i "+
		"JOIN pg_class c ON c.oid = i.indexrelid "+
		"JOIN pg_namespace n ON n.oid = c.relnamespace "+
		"WHERE n.nspname = current_schema() AND c.relname = $1", name).Scan(&valid)
	if err != nil {
		t.Fatalf("query indisvalid for %s: %v", name, err)
	}
	return valid
}

// seedDayIndexRows 写入足够让规划器不再选 Seq Scan 的行，并刷新统计。
// 20 账号 × 5 任务 × 120 天 = 12000 行，四种状态均匀分布。
func seedDayIndexRows(t *testing.T, m *migrationDownEnv) {
	t.Helper()
	m.exec("INSERT INTO users (email, password_hash) VALUES ('mig32@example.com', 'argon2id$test')")
	m.exec("INSERT INTO schools (code, name) VALUES ('mig32', 'Migration 32')")
	m.exec("INSERT INTO accounts (school_id, owner_user_id, phone) " +
		"SELECT s.id, u.id, '137' || lpad(g::text, 8, '0') " +
		"FROM schools s, users u, generate_series(1, 20) g")
	m.exec("INSERT INTO school_tasks (school_id, name) " +
		"SELECT id, 'day-task-' || g FROM schools, generate_series(1, 5) g")
	m.exec("INSERT INTO sign_tasks (account_id, school_task_id, day, run_at, state, attempts, result) " +
		"SELECT a.id, st.id, d.day, d.run_at, " +
		"(ARRAY['success','failed','pending','skipped'])[1 + (d.g % 4)]::sign_task_state, 0, " +
		"CASE WHEN d.g % 4 = 1 " +
		"THEN (ARRAY['timeout','auth_failed','unknown'])[1 + ((d.g / 4) % 3)] " +
		"ELSE '' END " +
		"FROM accounts a " +
		"JOIN schools sc ON sc.id = a.school_id " +
		"JOIN school_tasks st ON st.school_id = sc.id " +
		"CROSS JOIN LATERAL ( " +
		"SELECT g, DATE '2026-09-01' + (g - 1) AS day, " +
		"TIMESTAMPTZ '2026-09-01 06:00:00+00' + ((g - 1) * interval '1 day') " +
		"+ ((ascii(right(a.phone, 1)) - 48) * interval '1 minute') AS run_at " +
		"FROM generate_series(1, 120) g" +
		") d")
	if got := m.scalarInt("SELECT count(*) FROM sign_tasks"); got != 12000 {
		t.Fatalf("seeded sign_tasks rows = %d, want 12000", got)
	}
	for _, table := range []string{"sign_tasks", "accounts", "school_tasks"} {
		m.exec("ANALYZE " + table)
	}
}

// explainWithSeqscanOff 在事务里关掉 Seq Scan 跑 EXPLAIN (COSTS OFF)。
// enable_sort=off 迫使规划器走有序索引（StateCounts 的 GROUP BY state），
// 否则小表上可能选 HashAggregate + 另一条 day 索引。
func explainWithSeqscanOff(t *testing.T, m *migrationDownEnv, query string, args ...any) string {
	t.Helper()
	return explainPlan(t, m, []string{
		"SET LOCAL enable_seqscan = off",
		"SET LOCAL enable_sort = off",
	}, query, args...)
}

// explainPlain 不加任何规划器开关跑 EXPLAIN (COSTS OFF)：
// 用自然计划证明索引在日常参数下也会被选中，而不是只靠强制开关。
func explainPlain(t *testing.T, m *migrationDownEnv, query string, args ...any) string {
	t.Helper()
	return explainPlan(t, m, nil, query, args...)
}

// explainPlan 在事务里按需 SET LOCAL 后跑 EXPLAIN (COSTS OFF)。
func explainPlan(t *testing.T, m *migrationDownEnv, settings []string, query string, args ...any) string {
	t.Helper()
	tx, err := m.db.BeginTx(m.ctx, nil)
	if err != nil {
		t.Fatalf("begin explain tx: %v", err)
	}
	defer func() { _ = tx.Rollback() }()
	for _, stmt := range settings {
		if _, err := tx.ExecContext(m.ctx, stmt); err != nil {
			t.Fatalf("%s: %v", stmt, err)
		}
	}
	rows, err := tx.QueryContext(m.ctx, "EXPLAIN (COSTS OFF) "+query, args...)
	if err != nil {
		t.Fatalf("explain: %v", err)
	}
	defer rows.Close()
	var b strings.Builder
	for rows.Next() {
		var line string
		if err := rows.Scan(&line); err != nil {
			t.Fatalf("scan explain: %v", err)
		}
		b.WriteString(line)
		b.WriteString("\n")
	}
	if err := rows.Err(); err != nil {
		t.Fatalf("iterate explain: %v", err)
	}
	return b.String()
}

// wantSignTasksIndexPlan 断言 sign_tasks 自身走指定索引、未被 Seq Scan。
// 自然计划下小维表（accounts/school_tasks/schools）允许 Seq Scan：
// 它们只有几十行，强制走索引反而更慢；测试要钉的是 sign_tasks 的访问路径。
func wantSignTasksIndexPlan(t *testing.T, label, plan, index string) {
	t.Helper()
	if strings.Contains(plan, "Seq Scan on sign_tasks") {
		t.Fatalf("%s plan seq-scans sign_tasks; want %s:\n%s", label, index, plan)
	}
	if !strings.Contains(plan, index) {
		t.Fatalf("%s plan misses %s:\n%s", label, index, plan)
	}
	t.Logf("%s plan:\n%s", label, plan)
}

// wantIndexPlan 断言计划无 Seq Scan 且命中指定索引。
func wantIndexPlan(t *testing.T, label, plan, index string) {
	t.Helper()
	if strings.Contains(plan, "Seq Scan") {
		t.Fatalf("%s plan has Seq Scan; want %s:\n%s", label, index, plan)
	}
	if !strings.Contains(plan, index) {
		t.Fatalf("%s plan misses %s:\n%s", label, index, plan)
	}
	t.Logf("%s plan:\n%s", label, plan)
}
