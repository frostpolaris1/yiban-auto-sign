-- 00026_notification_deliveries: 邮件投递账本。
--
-- 一行 = 一条站内信对一个收件人的一个渠道投递。站内信不建行：
-- 通知行本身即已投。channel 当前只有 email；status 是
-- pending|sent|failed|skipped，取值由投递用例校验，本表不加 CHECK。
-- last_error_kind 只存 core/mail.Kind 词表；skip_reason 只存跳过原因
-- （如 smtp_not_configured），两者分工固定，不混装。
-- (notification_id, channel, user_id) 唯一：重复事件与并发双插只留一行。
-- due 部分索引只覆盖 pending，后台循环按 next_retry_at 升序扫描。
-- 通知或用户硬删时级联删除本表行；机器投递不写审计链。
--
-- Governance: migrations are forward-only. There is no Down section.

-- +goose Up
CREATE TABLE notification_deliveries (
    id              uuid PRIMARY KEY DEFAULT uuidv7(),
    notification_id uuid NOT NULL REFERENCES notifications (id) ON DELETE CASCADE,
    user_id         uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    channel         text NOT NULL,
    status          text NOT NULL DEFAULT 'pending',
    attempts        integer NOT NULL DEFAULT 0,
    last_error_kind text NOT NULL DEFAULT '',
    skip_reason     text NOT NULL DEFAULT '',
    next_retry_at   timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_notification_deliveries UNIQUE (notification_id, channel, user_id)
);

CREATE INDEX idx_deliveries_due ON notification_deliveries (next_retry_at)
    WHERE status = 'pending';
