-- 00023_notification_prefs: 每用户通知订阅覆盖集。
--
-- 一行 = (user, category, channel) 的显式覆盖值。缺行按默认值解释：
-- review_pending=in_app+email；credential_invalid=in_app；
-- sign_failed_daily=email；capacity_rejected=in_app。
-- 默认值定义在 core/notify；本表只存覆盖，不存默认，
-- 故 PUT 清空（删全部行）即可整体回到默认。
-- 类别与渠道是 core/notify 的闭集词表；入库值都经用例校验，
-- 本表不再加 CHECK（同一不变式只留一道守卫，见迁移 00021 的取舍）。
--
-- 本批只存取订阅；投递（站内信、邮件）随后续批次落地。
-- 同批补审计只读列表的排序索引：列表按 (ts DESC, id DESC) 键集分页，
-- actor 过滤按写入时固定的掩码标签精确匹配。
--
-- Governance: migrations are forward-only. There is no Down section.

-- +goose Up
CREATE TABLE notification_prefs (
    user_id  uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    category text NOT NULL,
    channel  text NOT NULL,
    enabled  boolean NOT NULL,
    PRIMARY KEY (user_id, category, channel)
);

-- 审计列表的排序与游标完全对应；缺索引时每页都要全表排序。
CREATE INDEX idx_audit_ts_id ON audit_logs (ts DESC, id DESC);

-- actor 过滤列是 actor_label（写入时已掩码），不是 actor_user_id。
CREATE INDEX idx_audit_actor_label_ts ON audit_logs (actor_label, ts DESC, id DESC);
