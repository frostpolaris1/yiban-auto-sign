-- 00029_auto_approve_setting: 全局自动审批开关。
--
-- 默认关闭：自动审批会把 pending 账号直接放进排期，误判代价高。
-- 行随迁移写入，读路径因此总能拿到值；键缺失时也按默认（关闭）处理。
-- 值只存小写 true/false 文本；其他值一律按关闭处理，不猜测。
--
-- Governance: migrations are forward-only, but this is a plain row drop,
-- so a Down section keeps Up -> Down -> Up exercisable like 00028.

-- +goose Up
INSERT INTO settings (key, value) VALUES ('auto_approve', 'false');

-- +goose Down
DELETE FROM settings WHERE key = 'auto_approve';
