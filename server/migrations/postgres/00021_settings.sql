-- 00021_settings: 全局运行时设置。
--
-- 公开注册默认关闭：新部署直接开放注册会招来批量垃圾账号。
-- 默认行随迁移写入，读路径因此总能拿到值；键缺失时也按默认（关闭）处理。
-- 值只存小写 true/false 文本；其他值一律按关闭处理，不猜测。
--
-- Governance: migrations are forward-only. There is no Down section.

-- +goose Up
CREATE TABLE settings (
    key        text PRIMARY KEY,
    value      text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by uuid REFERENCES users (id) ON DELETE SET NULL,
    CONSTRAINT ck_settings_key_shape CHECK (key ~ '^[a-z][a-z0-9_.]*$')
);

CREATE TRIGGER trg_settings_updated_at BEFORE UPDATE ON settings
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

INSERT INTO settings (key, value) VALUES ('registration.enabled', 'false');
