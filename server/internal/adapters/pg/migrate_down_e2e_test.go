package pg_test

// 迁移 Down 的真库 e2e。
//
// # 功能
// 每个带 Down 段的迁移执行 Up → Down → Up。
// Down 后断言目标表、类型、索引、约束确实消失。
// 00014 额外断言：window_cache 有 override 行时 Down 成功，该行降级为 cache。
//
// # 口径
// DDL 与约束的求值只在真 PostgreSQL 上成立；进程内替身不是证据（AGENTS §32）。
// goose 的 BaseFS 与 dialect 是进程级状态，故本文件不并行。
//
// # 归属
// 测试载体。被测对象是 migrations/postgres/*.sql 与 pg 的迁移执行面。

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"path"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/stdlib"
	"github.com/pressly/goose/v3"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	"github.com/frostpolaris1/yiban-auto-sign/server/migrations"
)

// irreversibleMarker 是「本迁移没有 Down 段」的文件头声明句。
// 无 Down 段的迁移必须在文件头写这一句；TestMigrationDownCoverage 检查它。
const irreversibleMarker = "There is no Down section."

// migrationDownCase 描述一个迁移的 Down 契约。
// seed 在 Down 之前把库变成有数据的形状。
// wantDown 在 Down 之后断言对象消失或数据按声明变化。
// wantUp 在重新 Up 之后断言对象恢复。
type migrationDownCase struct {
	name     string
	seed     func(m *migrationDownEnv)
	wantDown func(m *migrationDownEnv)
	wantUp   func(m *migrationDownEnv)
}

// migrationDownCases 是 Down 执行测试的唯一名册。
// 新增带 Down 段的迁移必须在此登记，否则 TestMigrationDownCoverage 红。
var migrationDownCases = map[int64]migrationDownCase{
	10: {
		name: "reject_reason_len",
		wantDown: func(m *migrationDownEnv) {
			m.wantConstraint("accounts", "ck_accounts_reject_reason_len", false)
		},
		wantUp: func(m *migrationDownEnv) {
			m.wantConstraint("accounts", "ck_accounts_reject_reason_len", true)
		},
	},
	11: {
		name: "session_hash_shape",
		wantDown: func(m *migrationDownEnv) {
			m.wantConstraint("sessions", "ck_sessions_ip_hash_shape", false)
			m.wantConstraint("sessions", "ck_sessions_user_agent_hash_shape", false)
		},
		wantUp: func(m *migrationDownEnv) {
			m.wantConstraint("sessions", "ck_sessions_ip_hash_shape", true)
			m.wantConstraint("sessions", "ck_sessions_user_agent_hash_shape", true)
		},
	},
	12: {
		name: "scheduling",
		seed: func(m *migrationDownEnv) {
			m.exec("INSERT INTO users (email, password_hash) VALUES ('mig-down-12@example.com', 'argon2id$test')")
			m.exec("INSERT INTO schools (code, name) VALUES ('mig-down-12', 'Migration Down')")
			m.exec("INSERT INTO school_tasks (school_id, name) SELECT id, 'mig-task' FROM schools")
			m.exec(`INSERT INTO accounts (school_id, owner_user_id, phone)
			        SELECT s.id, u.id, '13900000001' FROM schools s, users u`)
			m.exec(`INSERT INTO window_cache (school_task_id, day, start_at, end_at, source)
			        SELECT id, DATE '2026-10-27', TIMESTAMPTZ '2026-10-27 08:00:00+00',
			               TIMESTAMPTZ '2026-10-27 09:00:00+00', 'platform' FROM school_tasks`)
			m.exec(`INSERT INTO sign_tasks (account_id, school_task_id, day, run_at, state, attempts)
			        SELECT a.id, t.id, DATE '2026-10-27', TIMESTAMPTZ '2026-10-27 08:00:00+00', $1, 1
			        FROM accounts a, school_tasks t`, string(corestatus.TaskRunning))
			m.exec("INSERT INTO egresses (identity) VALUES ('mig-down-12-egress')")
		},
		wantDown: func(m *migrationDownEnv) {
			for _, table := range []string{"egresses", "sign_tasks", "window_cache", "school_tasks"} {
				m.wantRelation(table, false)
			}
			m.wantType("sign_task_state", false)
			for _, index := range []string{
				"uq_sign_tasks_one_running", "idx_sign_tasks_run_at", "idx_sign_tasks_claimed",
			} {
				m.wantRelation(index, false)
			}
		},
		wantUp: func(m *migrationDownEnv) {
			for _, table := range []string{"egresses", "sign_tasks", "window_cache", "school_tasks"} {
				m.wantRelation(table, true)
			}
			m.wantType("sign_task_state", true)
			for _, index := range []string{
				"uq_sign_tasks_one_running", "idx_sign_tasks_run_at", "idx_sign_tasks_claimed",
			} {
				m.wantRelation(index, true)
			}
			// 枚举复位后标签表必须可用。
			want := string(corestatus.TaskSuccess)
			if got := m.scalarString("SELECT $1::sign_task_state::text", want); got != want {
				m.t.Fatalf("sign_task_state label %q reads back as %q", want, got)
			}
		},
	},
	13: {
		name: "account_egress",
		seed: func(m *migrationDownEnv) {
			m.exec("INSERT INTO users (email, password_hash) VALUES ('mig-down-13@example.com', 'argon2id$test')")
			m.exec("INSERT INTO schools (code, name) VALUES ('mig-down-13', 'Migration Down')")
			m.exec(`INSERT INTO accounts (school_id, owner_user_id, phone)
			        SELECT s.id, u.id, '13900000002' FROM schools s, users u`)
			m.exec("INSERT INTO egresses (identity) VALUES ('mig-down-13-egress')")
			m.exec(`INSERT INTO account_egress (account_id, egress_id)
			        SELECT a.id, e.id FROM accounts a, egresses e`)
		},
		wantDown: func(m *migrationDownEnv) {
			m.wantRelation("account_egress", false)
			m.wantRelation("idx_account_egress_egress", false)
		},
		wantUp: func(m *migrationDownEnv) {
			m.wantRelation("account_egress", true)
			m.wantRelation("idx_account_egress_egress", true)
		},
	},
	14: {
		name: "time_prefs_overrides",
		seed: func(m *migrationDownEnv) {
			m.exec("INSERT INTO users (email, password_hash) VALUES ('mig-down-14@example.com', 'argon2id$test')")
			m.exec("INSERT INTO schools (code, name) VALUES ('mig-down-14', 'Migration Down')")
			m.exec("INSERT INTO school_tasks (school_id, name) SELECT id, 'mig-task' FROM schools")
			m.exec(`INSERT INTO window_cache (school_task_id, day, start_at, end_at, source)
			        SELECT id, DATE '2026-10-27', TIMESTAMPTZ '2026-10-27 07:00:00+00',
			               TIMESTAMPTZ '2026-10-27 08:00:00+00', 'override' FROM school_tasks`)
			m.exec(`INSERT INTO window_overrides (school_task_id, day, start_at, end_at, reason, created_by)
			        SELECT t.id, DATE '2026-10-27', TIMESTAMPTZ '2026-10-27 07:00:00+00',
			               TIMESTAMPTZ '2026-10-27 08:00:00+00', 'migration down seed', u.id
			        FROM school_tasks t, users u`)
			m.exec("INSERT INTO time_prefs (user_id, offset_min) SELECT id, 30 FROM users")
		},
		wantDown: func(m *migrationDownEnv) {
			m.wantRelation("window_overrides", false)
			m.wantRelation("time_prefs", false)

			// Down 必须把 override 行降级为 cache。
			// 留下任何 override 行都会让收窄语句失败。
			if got := m.scalarString(
				"SELECT source FROM window_cache WHERE day = DATE '2026-10-27'"); got != "cache" {
				m.t.Fatalf("window_cache.source = %q after Down; want \"cache\"", got)
			}
			if n := m.scalarInt(
				"SELECT count(*) FROM window_cache WHERE source = 'override'"); n != 0 {
				m.t.Fatalf("%d row(s) still carry source 'override' after Down", n)
			}

			// 收窄后的约束必须拒绝 override，且由该约束拒绝。
			err := m.tryExec(
				"UPDATE window_cache SET source = 'override' WHERE day = DATE '2026-10-27'")
			var pgErr *pgconn.PgError
			if !errors.As(err, &pgErr) {
				m.t.Fatalf("override write after Down: want a PostgreSQL error, got %v", err)
			}
			if pgErr.Code != "23514" || pgErr.ConstraintName != "ck_window_cache_source" {
				m.t.Fatalf("override write after Down blocked by %s/%s; want 23514/ck_window_cache_source",
					pgErr.Code, pgErr.ConstraintName)
			}
		},
		wantUp: func(m *migrationDownEnv) {
			m.wantRelation("time_prefs", true)
			m.wantRelation("window_overrides", true)
			// 放宽后的约束必须再次接受 override。
			m.exec("UPDATE window_cache SET source = 'override' WHERE day = DATE '2026-10-27'")
		},
	},
	15: {
		name: "photos",
		seed: func(m *migrationDownEnv) {
			m.exec("INSERT INTO users (email, password_hash) VALUES ('mig-down-15@example.com', 'argon2id$test')")
			m.exec(`INSERT INTO photos (owner_user_id, sha256, mime, width, height, bytes)
			        SELECT id, decode(repeat('ab', 32), 'hex'), 'image/jpeg', 1, 1, 1 FROM users`)
			m.exec("INSERT INTO photo_retention (user_id, days) SELECT id, 90 FROM users")
		},
		wantDown: func(m *migrationDownEnv) {
			m.wantRelation("photos", false)
			m.wantRelation("photo_retention", false)
			m.wantRelation("idx_photos_owner_created", false)
			m.wantRelation("idx_photos_created", false)
		},
		wantUp: func(m *migrationDownEnv) {
			m.wantRelation("photos", true)
			m.wantRelation("photo_retention", true)
			m.wantRelation("idx_photos_owner_created", true)
			m.wantRelation("idx_photos_created", true)
		},
	},
	16: {
		name: "account_location",
		seed: func(m *migrationDownEnv) {
			m.exec("INSERT INTO users (email, password_hash) VALUES ('mig-down-16@example.com', 'argon2id$test')")
			m.exec("INSERT INTO schools (code, name) VALUES ('mig-down-16', 'Migration Down')")
			m.exec(`INSERT INTO accounts (school_id, owner_user_id, phone)
			        SELECT s.id, u.id, '13900000003' FROM schools s, users u`)
			m.exec(`INSERT INTO account_location (account_id, mode, lng, lat, reason)
			        SELECT id, 'fixed', 0.5, 0.5, 'migration down seed' FROM accounts`)
		},
		wantDown: func(m *migrationDownEnv) {
			m.wantRelation("account_location", false)
		},
		wantUp: func(m *migrationDownEnv) {
			m.wantRelation("account_location", true)
		},
	},
}

// TestMigrationDownIsExecutable 在真库上执行每个已登记的 Down。
// 顺序固定：先 Up 到该版本，再 Down 一步，再 Up 回去。
func TestMigrationDownIsExecutable(t *testing.T) {
	versions := make([]int64, 0, len(migrationDownCases))
	for version := range migrationDownCases {
		versions = append(versions, version)
	}
	sort.Slice(versions, func(i, j int) bool { return versions[i] < versions[j] })

	for _, version := range versions {
		tc := migrationDownCases[version]
		t.Run(fmt.Sprintf("%02d_%s", version, tc.name), func(t *testing.T) {
			m := newMigrationDownEnv(t)
			m.upTo(version)
			if tc.seed != nil {
				tc.seed(m)
			}
			m.downOne(version)
			tc.wantDown(m)
			// Down 之后再 Up：回滚后的 schema 必须能重新前进。
			m.upTo(version)
			tc.wantUp(m)
		})
	}
}

// TestMigrationDownCoverage 钉住 Down 与测试登记的一一对应。
// 有 Down 段无测试即红；无 Down 段且文件头无声明即红。
func TestMigrationDownCoverage(t *testing.T) {
	entries, err := migrations.Files.ReadDir(migrations.Dir)
	if err != nil {
		t.Fatalf("read embedded migrations: %v", err)
	}
	seen := make(map[int64]bool, len(entries))
	files := 0

	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".sql") {
			continue
		}
		name := entry.Name()
		prefix, _, ok := strings.Cut(name, "_")
		if !ok {
			t.Fatalf("migration %q has no numeric prefix", name)
		}
		version, err := strconv.ParseInt(prefix, 10, 64)
		if err != nil {
			t.Fatalf("migration %q prefix: %v", name, err)
		}
		if seen[version] {
			t.Fatalf("version %d appears in more than one migration file", version)
		}
		data, err := migrations.Files.ReadFile(path.Join(migrations.Dir, name))
		if err != nil {
			t.Fatalf("read %q: %v", name, err)
		}
		text := string(data)
		hasDown := hasDownSection(text)
		_, covered := migrationDownCases[version]

		switch {
		case hasDown && !covered:
			t.Errorf("migration %s has a Down section but no case in migrationDownCases", name)
		case !hasDown && covered:
			t.Errorf("migration %s has no Down section but migrationDownCases names it", name)
		case !hasDown && !strings.Contains(text, irreversibleMarker):
			t.Errorf("migration %s has no Down section and its header does not say %q",
				name, irreversibleMarker)
		}
		seen[version] = true
		files++
	}

	if files == 0 {
		t.Fatal("scanned zero migrations; the coverage gate would be vacuously green")
	}
	for version := range migrationDownCases {
		if !seen[version] {
			t.Errorf("migrationDownCases names version %d but no embedded file has it", version)
		}
	}
}

// hasDownSection 按 goose 的注解规则判断文件是否带 Down 段。
// goose 把以 "--" 开头且含 "+goose" 的注释行都当注解（internal/sqlparser），
// 故不能用固定字面量匹配：门识别的形状必须等于执行面（AGENTS §29）。
func hasDownSection(text string) bool {
	for _, line := range strings.Split(text, "\n") {
		// goose 拒绝带前导空白的注解行；那种行不是可执行的 Down 段。
		if strings.HasPrefix(line, " ") || strings.HasPrefix(line, "\t") {
			continue
		}
		trimmed := strings.TrimSpace(line)
		if !strings.HasPrefix(trimmed, "--") || !strings.Contains(line, "+goose") {
			continue
		}
		cmd := strings.ReplaceAll(line, "--", "")
		cmd = strings.Replace(cmd, "+goose", "", 1)
		if strings.TrimSpace(cmd) == "Down" {
			return true
		}
	}
	return false
}

// TestDownAnnotationRecognition 钉住 hasDownSection 与 goose 的识别规则一致。
func TestDownAnnotationRecognition(t *testing.T) {
	for _, tc := range []struct {
		text string
		want bool
	}{
		{"-- +goose Down", true},
		{"--+goose Down", true},
		{"-- +goose  Down", true},
		{"-- +goose Up", false},
		{"-- Governance: migrations are forward-only. There is no Down section.", false},
		{"  -- +goose Down", false},
	} {
		if got := hasDownSection(tc.text); got != tc.want {
			t.Errorf("hasDownSection(%q) = %v; want %v", tc.text, got, tc.want)
		}
	}
}

// migrationDownEnv 独占一个数据库 schema，用 goose 直接驱动 Up 与 Down。
type migrationDownEnv struct {
	t   *testing.T
	ctx context.Context
	db  *sql.DB
}

func newMigrationDownEnv(t *testing.T) *migrationDownEnv {
	t.Helper()
	base := requireDSN(t)
	ctx := t.Context()
	schema := "pgmig_" + randomHex(8)

	admin, err := pg.Open(ctx, pg.Options{DSN: base, MaxConns: 1, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open admin connection: %v", err)
	}
	if _, err := admin.Exec(ctx, "CREATE SCHEMA "+schema); err != nil {
		admin.Close()
		t.Fatalf("create schema %s: %v", schema, err)
	}

	db := openGooseDB(t, withSchema(base, schema))
	t.Cleanup(func() {
		db.Close()
		dropSchema(t, admin, schema)
		admin.Close()
	})

	// goose 的 BaseFS 与 dialect 是进程级状态；产品 Migrate 设同一组值。
	goose.SetBaseFS(migrations.Files)
	if err := goose.SetDialect("postgres"); err != nil {
		t.Fatalf("goose dialect: %v", err)
	}
	return &migrationDownEnv{t: t, ctx: ctx, db: db}
}

// openGooseDB 建单连接 *sql.DB，参数与产品 Migrate 相同。
func openGooseDB(t *testing.T, dsn string) *sql.DB {
	t.Helper()
	cfg, err := pgx.ParseConfig(dsn)
	if err != nil {
		t.Fatalf("parse migration DSN: %v", err)
	}
	if cfg.RuntimeParams == nil {
		cfg.RuntimeParams = map[string]string{}
	}
	cfg.RuntimeParams["timezone"] = "UTC"
	cfg.RuntimeParams["lock_timeout"] = "5s"
	cfg.RuntimeParams["statement_timeout"] = "60s"
	db := stdlib.OpenDB(*cfg)
	db.SetMaxOpenConns(1)
	return db
}

func (m *migrationDownEnv) upTo(version int64) {
	m.t.Helper()
	if err := goose.UpToContext(m.ctx, m.db, migrations.Dir, version); err != nil {
		m.t.Fatalf("goose up to %d: %v", version, err)
	}
	m.wantVersion(version)
}

func (m *migrationDownEnv) downOne(version int64) {
	m.t.Helper()
	if err := goose.DownContext(m.ctx, m.db, migrations.Dir); err != nil {
		m.t.Fatalf("goose down from %d: %v", version, err)
	}
	m.wantVersion(version - 1)
}

func (m *migrationDownEnv) wantVersion(want int64) {
	m.t.Helper()
	got, err := goose.GetDBVersionContext(m.ctx, m.db)
	if err != nil {
		m.t.Fatalf("read schema version: %v", err)
	}
	if got != want {
		m.t.Fatalf("schema version is %d; want %d", got, want)
	}
}

func (m *migrationDownEnv) exec(sql string, args ...any) {
	m.t.Helper()
	if _, err := m.db.ExecContext(m.ctx, sql, args...); err != nil {
		m.t.Fatalf("exec %q: %v", sql, err)
	}
}

// tryExec 返回错误供断言使用，不终止测试。
func (m *migrationDownEnv) tryExec(sql string, args ...any) error {
	m.t.Helper()
	_, err := m.db.ExecContext(m.ctx, sql, args...)
	return err
}

func (m *migrationDownEnv) scalarString(sql string, args ...any) string {
	m.t.Helper()
	var out string
	if err := m.db.QueryRowContext(m.ctx, sql, args...).Scan(&out); err != nil {
		m.t.Fatalf("query %q: %v", sql, err)
	}
	return out
}

func (m *migrationDownEnv) scalarInt(sql string, args ...any) int64 {
	m.t.Helper()
	var out int64
	if err := m.db.QueryRowContext(m.ctx, sql, args...).Scan(&out); err != nil {
		m.t.Fatalf("query %q: %v", sql, err)
	}
	return out
}

func (m *migrationDownEnv) hasRelation(name string) bool {
	m.t.Helper()
	var exists bool
	if err := m.db.QueryRowContext(m.ctx,
		"SELECT to_regclass($1) IS NOT NULL", name).Scan(&exists); err != nil {
		m.t.Fatalf("to_regclass(%q): %v", name, err)
	}
	return exists
}

func (m *migrationDownEnv) hasType(name string) bool {
	m.t.Helper()
	var exists bool
	if err := m.db.QueryRowContext(m.ctx,
		"SELECT to_regtype($1) IS NOT NULL", name).Scan(&exists); err != nil {
		m.t.Fatalf("to_regtype(%q): %v", name, err)
	}
	return exists
}

func (m *migrationDownEnv) hasConstraint(table, name string) bool {
	m.t.Helper()
	var exists bool
	if err := m.db.QueryRowContext(m.ctx, `SELECT EXISTS (
	        SELECT 1 FROM pg_constraint
	        WHERE conname = $2 AND conrelid = to_regclass($1)::oid)`,
		table, name).Scan(&exists); err != nil {
		m.t.Fatalf("constraint %s on %s: %v", name, table, err)
	}
	return exists
}

func (m *migrationDownEnv) wantRelation(name string, want bool) {
	m.t.Helper()
	if got := m.hasRelation(name); got != want {
		m.t.Fatalf("relation %q exists = %v; want %v", name, got, want)
	}
}

func (m *migrationDownEnv) wantType(name string, want bool) {
	m.t.Helper()
	if got := m.hasType(name); got != want {
		m.t.Fatalf("type %q exists = %v; want %v", name, got, want)
	}
}

func (m *migrationDownEnv) wantConstraint(table, name string, want bool) {
	m.t.Helper()
	if got := m.hasConstraint(table, name); got != want {
		m.t.Fatalf("constraint %q on %q exists = %v; want %v", name, table, got, want)
	}
}
