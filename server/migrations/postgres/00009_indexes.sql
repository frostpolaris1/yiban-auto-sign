-- 00009_indexes: every secondary index of the P1 schema.
--
-- Unique constraints declared inside a CREATE TABLE stay there. This file
-- holds the partial unique indexes and the query-support indexes, in table
-- order. Names match the schema design; do not rename them: error mapping and
-- later migrations locate constraints by name.

-- +goose Up
-- users
CREATE UNIQUE INDEX uq_users_email_live ON users (email)
    WHERE deleted_at IS NULL;
CREATE INDEX idx_users_role_live ON users (role, created_at DESC, id DESC)
    WHERE deleted_at IS NULL;

-- sessions
CREATE INDEX idx_sessions_user_live ON sessions (user_id)
    WHERE revoked_at IS NULL;
CREATE INDEX idx_sessions_expiry ON sessions (expires_at);

-- schools
CREATE INDEX idx_schools_status ON schools (status, code);

-- admin_schools
CREATE INDEX idx_admin_schools_school ON admin_schools (school_id, user_id);

-- accounts
CREATE UNIQUE INDEX uq_accounts_school_phone_live
    ON accounts (school_id, phone) WHERE deleted_at IS NULL;
CREATE INDEX idx_accounts_school_created
    ON accounts (school_id, created_at DESC, id DESC) WHERE deleted_at IS NULL;
CREATE INDEX idx_accounts_school_status
    ON accounts (school_id, status, created_at DESC, id DESC) WHERE deleted_at IS NULL;
CREATE INDEX idx_accounts_owner
    ON accounts (owner_user_id, created_at DESC, id DESC) WHERE deleted_at IS NULL;

-- credentials
CREATE INDEX idx_credentials_kid ON credentials (kid);

-- password_resets
CREATE INDEX idx_password_resets_user ON password_resets (user_id, created_at DESC);
CREATE INDEX idx_password_resets_expiry ON password_resets (expires_at);

-- audit_logs
CREATE INDEX idx_audit_school_ts ON audit_logs (school_id, ts DESC, id DESC);
CREATE INDEX idx_audit_actor_ts ON audit_logs (actor_user_id, ts DESC, id DESC);
CREATE INDEX idx_audit_action_ts ON audit_logs (action, ts DESC, id DESC);
CREATE INDEX idx_audit_target_ts ON audit_logs (target_type, target_id, ts DESC);
