-- 00024_mail_notify: 每用户签到失败邮件开关。
--
-- 一行一个布尔值，缺省 true（旧线 parity：users.mail_notify 默认 1）。
-- 开关只影响签到失败邮件的投递判断；投递本身（0v8）尚未落地，
-- 本批只存取取值并写审计。
-- 开关属于用户行，不建表：取值只有一份，不复制到 notification_prefs。
--
-- Governance: migrations are forward-only. There is no Down section.

-- +goose Up
ALTER TABLE users ADD COLUMN mail_notify boolean NOT NULL DEFAULT true;
