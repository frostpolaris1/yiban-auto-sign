-- 00022_account_name: 账号备注名。
--
-- 备注名是账号的可读标识，不参与登录、审核与调度判定。
-- 空串表示未命名；长度按字符计，上限 50（与旧线同口径）。
--
-- Governance: migrations are forward-only. There is no Down section.

-- +goose Up
ALTER TABLE accounts ADD COLUMN name text NOT NULL DEFAULT '';

ALTER TABLE accounts ADD CONSTRAINT ck_accounts_name_length
    CHECK (char_length(name) <= 50);
