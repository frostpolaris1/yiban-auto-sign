-- +goose NO TRANSACTION

-- Governance: migrations are forward-only. There is no Down section.

-- P2-9：全局账号列表的局部索引。
--
-- 病根：master scope、不带 school 过滤的账号列表是全局倒序页
-- （WHERE deleted_at IS NULL ORDER BY created_at DESC, id DESC LIMIT n）。
-- 00009 建的是 (school_id, ...) 与 (owner_user_id, ...) 开头的前缀索引，
-- 这个形状只能全表扫描再排序。
--
-- 做法：补一条同形的局部索引，只覆盖未软删账号，与列表的排序键一一对应。
-- 用 CREATE INDEX CONCURRENTLY 建，不阻塞账号写入；CIC 不能在事务块内
-- 运行，故本文件 NO TRANSACTION。
--
-- 锁超时：CIC 的第一阶段要短暂持有 SHARE UPDATE EXCLUSIVE，会排在未结束
-- 事务后面。窗口内把 lock_timeout 置 0，避免建到一半被取消；结束后恢复
-- 5s，与 pg/migrate.go 的默认值一致。本迁移只动锁超时，不调整语句超时。
--
-- 失败恢复：CIC 被中断会留下 INVALID 索引，按 deploy/README.md §7.1
-- 只重建 INVALID 的那一条；本迁移可重复执行（IF NOT EXISTS）。

-- +goose Up
SET lock_timeout='0';
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_accounts_created_live ON accounts (created_at DESC, id DESC) WHERE deleted_at IS NULL;
SET lock_timeout='5s';
