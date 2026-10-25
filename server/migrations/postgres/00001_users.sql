-- 00001_users: foundation and the users table.
--
-- The foundation parts are here because users needs them:
--   * a PostgreSQL 18 check, because uuidv7() and the schema assume 18;
--   * the user_role and user_status enums;
--   * set_updated_at(), used by every table with an updated_at column.
--
-- Governance: migrations are forward-only. There is no Down section.

-- +goose Up
-- +goose StatementBegin
DO $$
BEGIN
    IF current_setting('server_version_num')::integer < 180000 THEN
        RAISE EXCEPTION 'PostgreSQL 18 or newer is required; found %',
            current_setting('server_version');
    END IF;
END
$$;
-- +goose StatementEnd

CREATE TYPE user_role AS ENUM ('master', 'admin', 'user');
CREATE TYPE user_status AS ENUM ('active', 'disabled');

-- +goose StatementBegin
CREATE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END
$$;
-- +goose StatementEnd

CREATE TABLE users (
    id                   uuid PRIMARY KEY DEFAULT uuidv7(),
    email                text NOT NULL,
    password_hash        text NOT NULL,
    role                 user_role NOT NULL DEFAULT 'user',
    status               user_status NOT NULL DEFAULT 'active',
    must_change_password boolean NOT NULL DEFAULT false,
    password_changed_at  timestamptz,
    last_login_at        timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    deleted_at           timestamptz,
    deleted_by           uuid REFERENCES users (id) ON DELETE SET NULL,
    CONSTRAINT ck_users_email_lower CHECK (email = lower(email)),
    CONSTRAINT ck_users_email_shape CHECK (
        email ~ '^[^[:space:]@]+@[^[:space:]@]+$'
        AND position('*' in email) = 0
        AND char_length(email) BETWEEN 3 AND 254
    ),
    CONSTRAINT ck_users_deleted_by CHECK (deleted_at IS NOT NULL OR deleted_by IS NULL)
);

CREATE TRIGGER trg_users_updated_at BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
