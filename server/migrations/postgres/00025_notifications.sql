-- 00025_notifications: 站内信。
--
-- 一行 = 一个收件人的一条站内信。category 是 core/notify 的闭合词表，
-- 入库前由用例校验；本表不再加 CHECK（同一不变式只留一道守卫）。
-- (user_id, dedupe_key) 唯一：同一业务事件重复投递只落一行，
-- 重放与并发都不产生第二条。
-- 列表按 (created_at DESC, id DESC) 键集分页；未读数只扫 read_at IS NULL。
-- 保留期 30 天，由 app/retention 按 created_at 分批删除。
-- 用户硬删时级联删除本表行；投递账本见 00026，随本表级联删除。
-- 本表不存邮箱；标题与正文入库前由用例掩码手机号等敏感值。
--
-- Governance: migrations are forward-only. There is no Down section.

-- +goose Up
CREATE TABLE notifications (
    id         uuid PRIMARY KEY DEFAULT uuidv7(),
    user_id    uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    category   text NOT NULL,
    dedupe_key text NOT NULL,
    title      text NOT NULL,
    body       text NOT NULL DEFAULT '',
    school_id  uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    read_at    timestamptz,
    CONSTRAINT uq_notifications_user_dedupe UNIQUE (user_id, dedupe_key)
);

-- 列表排序与游标完全对应；缺索引时每页都要全表排序。
CREATE INDEX idx_notifications_page ON notifications (user_id, created_at DESC, id DESC);

-- 未读数只关心未读行；部分索引不随已读行增长。
CREATE INDEX idx_notifications_unread ON notifications (user_id) WHERE read_at IS NULL;
