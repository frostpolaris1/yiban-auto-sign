-- +goose NO TRANSACTION
--
-- 00032_sign_tasks_day_indexes: 按日列表/计数与统计目标（P0-3）。
--
-- 病根：sign_tasks 只有 (account_id, school_task_id, day) 唯一键与
-- run_at/claimed_at/lease_expires_at 三条局部索引；按 day 过滤的列表、
-- 状态计数、失败分布都没有可用前缀。日切构建后统计也只靠 autovacuum
-- 默认阈值（50 行 + 20%），任务行日增后统计长期滞后，计划容易走偏。
--
-- 做法：
--   1) 两个索引都带 day 前缀：(day, run_at DESC, id DESC) 供 ListTasks
--      的倒序分页（keyset 形状），(day, state) 供 StateCounts/FailureCounts
--      的按日分组与等值过滤；
--   2) 把该表的 autovacuum analyze 阈值收紧到 500 行 / 1%，让日切后
--      新行尽快进入统计；
--   3) 迁移末尾 ANALYZE 一次，给新索引与统计一个起点。
--
-- 锁超时窗口：迁移连接由 openMigrationDB 建，自带 lock_timeout=5s。
-- ALTER 保留 5s 的快速失败语义；两条 CIC 在等待老快照时可能超过 5s，
-- 故 ALTER 之后、首个 CIC 之前 SET lock_timeout='0'，建完索引后、
-- ANALYZE 之前收回 '5s'（ANALYZE 也要快速失败），末尾恢复
-- statement_timeout。lock_timeout=0 只覆盖真正需要长等的 CIC。
--
-- 注意：CREATE INDEX CONCURRENTLY 不在事务里；中断会留下 INVALID 索引
--（pg_index.indisvalid = false）。CIC 当场失败时 goose 通常还没写版本行，
-- 重跑会再次执行本迁移；危险在重跑时 IF NOT EXISTS 看到同名 INVALID
-- 直接跳过，随后版本落库为 32。版本一旦记录，goose 不再执行本文件，
-- 重跑 Migrate 不会修复 INVALID，需按 deploy/README.md
-- 「00032 索引失败处理」手工重建（不要无条件删两条）。
--
-- 迁移窗口不要同时跑 pg_dump / 长事务：旧快照会让 CIC 的等待阶段卡住。
--
-- Governance: migrations are forward-only. There is no Down section.

-- +goose Up
ALTER TABLE sign_tasks SET (
    autovacuum_analyze_scale_factor = 0.01,
    autovacuum_analyze_threshold = 500
);

SET statement_timeout = '0';
SET lock_timeout = '0';

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sign_tasks_day_run_at_id
    ON sign_tasks (day, run_at DESC, id DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sign_tasks_day_state
    ON sign_tasks (day, state);

-- 与 server/internal/adapters/pg/migrate.go:101 的 lock_timeout=5s 保持同步；
-- 改一处必须改另一处，否则 CIC 窗口后的行为会漂移。
SET lock_timeout = '5s';

ANALYZE sign_tasks;

RESET statement_timeout;
