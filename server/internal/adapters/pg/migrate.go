package pg

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"path"
	"strconv"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/stdlib"
	"github.com/pressly/goose/v3"

	"github.com/frostpolaris1/yiban-auto-sign/server/migrations"
)

// ErrDatabaseAhead reports that the database schema version is newer than the
// version this binary carries. A downgraded process must stop: it does not
// know the newer columns or constraints (schema §9.3).
var ErrDatabaseAhead = errors.New("pg: database schema is newer than this binary")

// Migrate applies every pending migration and returns the resulting schema
// version.
//
// The migration session gets lock_timeout=5s and statement_timeout=60s. A
// migration that cannot take its lock fails instead of queueing and blocking
// live traffic.
func Migrate(ctx context.Context, dsn string) (int64, error) {
	embedded, err := EmbeddedVersion()
	if err != nil {
		return 0, err
	}
	db, err := openMigrationDB(dsn)
	if err != nil {
		return 0, err
	}
	defer db.Close()

	current, err := dbVersion(ctx, db)
	if err != nil {
		return 0, err
	}
	if current > embedded {
		return current, fmt.Errorf("%w: database %d, binary %d", ErrDatabaseAhead, current, embedded)
	}

	goose.SetBaseFS(migrations.Files)
	if err := goose.SetDialect("postgres"); err != nil {
		return 0, fmt.Errorf("pg: goose dialect: %w", err)
	}
	if err := goose.UpContext(ctx, db, migrations.Dir); err != nil {
		return 0, fmt.Errorf("pg: migrate up: %w", err)
	}
	version, err := goose.GetDBVersionContext(ctx, db)
	if err != nil {
		return 0, fmt.Errorf("pg: read schema version: %w", err)
	}
	return version, nil
}

// EmbeddedVersion returns the highest migration number compiled into this
// binary. It reads the embedded file names, so a missing embed shows up here.
func EmbeddedVersion() (int64, error) {
	entries, err := migrations.Files.ReadDir(migrations.Dir)
	if err != nil {
		return 0, fmt.Errorf("pg: read embedded migrations: %w", err)
	}
	var highest int64
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".sql") {
			continue
		}
		base := path.Base(entry.Name())
		prefix, _, ok := strings.Cut(base, "_")
		if !ok {
			return 0, fmt.Errorf("pg: migration %q has no numeric prefix", entry.Name())
		}
		version, err := strconv.ParseInt(prefix, 10, 64)
		if err != nil {
			return 0, fmt.Errorf("pg: migration %q prefix: %w", entry.Name(), err)
		}
		if version > highest {
			highest = version
		}
	}
	if highest == 0 {
		return 0, errors.New("pg: no embedded migrations found")
	}
	return highest, nil
}

// openMigrationDB builds a single-connection sql.DB with migration timeouts.
func openMigrationDB(dsn string) (*sql.DB, error) {
	cfg, err := pgx.ParseConfig(dsn)
	if err != nil {
		return nil, fmt.Errorf("pg: parse migration DSN: %w", err)
	}
	if cfg.RuntimeParams == nil {
		cfg.RuntimeParams = map[string]string{}
	}
	cfg.RuntimeParams["timezone"] = "UTC"
	cfg.RuntimeParams["lock_timeout"] = "5s"
	cfg.RuntimeParams["statement_timeout"] = "60s"
	db := stdlib.OpenDB(*cfg)
	db.SetMaxOpenConns(1)
	return db, nil
}

// dbVersion reads the applied version, treating a missing version table as
// version 0 (a fresh database).
func dbVersion(ctx context.Context, db *sql.DB) (int64, error) {
	var exists bool
	if err := db.QueryRowContext(ctx,
		"SELECT to_regclass('goose_db_version') IS NOT NULL").Scan(&exists); err != nil {
		return 0, fmt.Errorf("pg: check migration table: %w", err)
	}
	if !exists {
		return 0, nil
	}
	var version int64
	if err := db.QueryRowContext(ctx,
		"SELECT coalesce(max(version_id), 0) FROM goose_db_version WHERE is_applied").Scan(&version); err != nil {
		return 0, fmt.Errorf("pg: read schema version: %w", err)
	}
	return version, nil
}
