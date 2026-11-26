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
-- 注意：CREATE INDEX CONCURRENTLY 不在事务里；中断会留下 INVALID 索引
--（pg_index.indisvalid = false）。重跑前必须先
-- DROP INDEX CONCURRENTLY IF EXISTS 清理（见 deploy/README.md
-- 「00032 索引失败处理」）。IF NOT EXISTS 只跳过同名对象，不修复 INVALID。
--
-- Governance: migrations are forward-only. There is no Down section.

-- +goose Up
ALTER TABLE sign_tasks SET (
    autovacuum_analyze_scale_factor = 0.01,
    autovacuum_analyze_threshold = 500
);

SET statement_timeout = '0';

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sign_tasks_day_run_at_id
    ON sign_tasks (day, run_at DESC, id DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sign_tasks_day_state
    ON sign_tasks (day, state);

ANALYZE sign_tasks;

RESET statement_timeout;
