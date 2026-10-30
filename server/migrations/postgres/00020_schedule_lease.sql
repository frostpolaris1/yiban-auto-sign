-- 00020: 执行租约。
--
-- running 行必须带到期时间：worker 崩溃、Finish 失败或连接断开时，
-- 过期的 running 由每 tick 的回收器放回 pending，账号不再被永久卡死。
-- 历史 running 行的租约为空；启动回收仍负责清一次。
--
-- 回收按 (lease_expires_at) 的 partial index 取行，只覆盖 running。
-- Governance: migrations are forward-only. There is no Down section.

-- +goose Up
ALTER TABLE sign_tasks ADD COLUMN lease_expires_at timestamptz;

CREATE INDEX idx_sign_tasks_lease ON sign_tasks (lease_expires_at)
    WHERE state = 'running';
