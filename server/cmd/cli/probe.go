// doctor 的生产数据库探针：pgHandle 把体检语义翻译成 SQL。
//
// # 口径
// 全部查询走 search_path：DSN 带 search_path=xxx 时，
// 表名按该 schema 解析（测试以 per-test schema 隔离）。
// Count 只接受本文件登记的固定形状，不接受拼接输入。
package main

import (
	"context"
	"fmt"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/assembly"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
)

// pgHandle 是 dbHandle 的生产实现。
type pgHandle struct {
	db     *pg.DB
	audits *pg.AuditRepo // pepper 不可用时为 nil：审计检查按 SKIP 处理
}

// openPgProbe 打开连接池并装配审计仓储（审计键由 pepper 派生）。
func openPgProbe(ctx context.Context, dsn string, pepper []byte) (dbHandle, error) {
	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 2, StatementTimeout: 10 * time.Second})
	if err != nil {
		return nil, err
	}
	h := &pgHandle{db: db}
	key, keyErr := assembly.DeriveAuditKey(pepper)
	if keyErr != nil {
		// pepper 缺失不是连接失败：连接可探，审计检查自行跳过。
		return h, nil
	}
	h.audits, err = pg.NewAuditRepo(db, key)
	if err != nil {
		db.Close()
		return nil, fmt.Errorf("audit repo: %w", err)
	}
	return h, nil
}

// Close 释放连接池。
func (h *pgHandle) Close() { h.db.Close() }

// SchemaVersion 读 goose 版本；版本表缺失视为 0（全新库）。
func (h *pgHandle) SchemaVersion(ctx context.Context) (int64, error) {
	ok, err := h.TableExists(ctx, "goose_db_version")
	if err != nil {
		return 0, err
	}
	if !ok {
		return 0, nil
	}
	return h.db.QueryInt64(ctx,
		"SELECT coalesce(max(version_id), 0) FROM goose_db_version WHERE is_applied")
}

// ServerVersionNum 读 server_version_num（如 180008 = 18.8）。
func (h *pgHandle) ServerVersionNum(ctx context.Context) (int64, error) {
	return h.db.QueryInt64(ctx, "SELECT current_setting('server_version_num')::int8")
}

// TableExists 按 search_path 判表存在。
func (h *pgHandle) TableExists(ctx context.Context, name string) (bool, error) {
	v, err := h.db.QueryString(ctx, "SELECT (to_regclass($1) IS NOT NULL)::text", name)
	if err != nil {
		return false, err
	}
	return v == "true", nil
}

// DBNow 读数据库当前时刻。会话时区固定 UTC（pg.Open 的口径）。
func (h *pgHandle) DBNow(ctx context.Context) (time.Time, error) {
	v, err := h.db.QueryString(ctx, "SELECT now()::text")
	if err != nil {
		return time.Time{}, err
	}
	return parseDBNow(v)
}

// parseDBNow 解析 PostgreSQL 的 now() 文本形状。
// 形状如 "2026-10-27 04:00:00.123456+00"（微秒可缺省）。
func parseDBNow(v string) (time.Time, error) {
	for _, layout := range []string{
		"2006-01-02 15:04:05.999999999-07",
		"2006-01-02 15:04:05-07",
	} {
		if t, err := time.Parse(layout, v); err == nil {
			return t, nil
		}
	}
	return time.Time{}, fmt.Errorf("doctor: unparsable now() text %q", v)
}

// Count 数表行数。只接受登记过的 (table, where) 组合，杜绝拼接输入。
func (h *pgHandle) Count(ctx context.Context, table, where string, args ...any) (int64, error) {
	var sql string
	switch {
	case table == "egresses" && where == "":
		sql = "SELECT count(*) FROM egresses"
	case table == "school_tasks" && where == "enabled":
		sql = "SELECT count(*) FROM school_tasks WHERE enabled"
	case table == "sign_tasks" && where == "state = $1":
		sql = "SELECT count(*) FROM sign_tasks WHERE state = $1"
	default:
		return 0, fmt.Errorf("doctor: unsupported count shape %q where %q", table, where)
	}
	return h.db.QueryInt64(ctx, sql, args...)
}

// AuditVerify 全链校验。audits 未装配（pepper 缺失）返回带原因的错误。
func (h *pgHandle) AuditVerify(ctx context.Context) (coreaudit.Report, error) {
	if h.audits == nil {
		return coreaudit.Report{}, fmt.Errorf("audit key unavailable: YIBAN_HASH_PEPPER missing")
	}
	// 0 表示两侧不限：整链校验。
	return h.audits.Verify(ctx, 0, 0)
}
