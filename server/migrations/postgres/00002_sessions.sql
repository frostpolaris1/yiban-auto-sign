-- 00002_sessions: server-side sessions.
--
-- Sessions are mutable: revocation is an UPDATE and expiry cleanup is a
-- DELETE. There is no csrf_hash column (decision 15.1): the CSRF token is
-- derived from the session token on each request and never stored.
--
-- Governance: migrations are forward-only. There is no Down section.

-- +goose Up
CREATE TABLE sessions (
    id              uuid PRIMARY KEY DEFAULT uuidv7(),
    user_id         uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    token_hash      bytea NOT NULL,
    auth_method     text NOT NULL DEFAULT 'password',
    created_at      timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    idle_expires_at timestamptz NOT NULL,
    expires_at      timestamptz NOT NULL,
    revoked_at      timestamptz,
    ip_hash         text,
    user_agent_hash text,
    CONSTRAINT uq_sessions_token UNIQUE (token_hash),
    CONSTRAINT ck_sessions_token_len CHECK (octet_length(token_hash) = 32),
    CONSTRAINT ck_sessions_expiry CHECK (idle_expires_at <= expires_at),
    CONSTRAINT ck_sessions_revoked CHECK (revoked_at IS NULL OR revoked_at >= created_at),
    CONSTRAINT ck_sessions_auth_method CHECK (auth_method IN ('password', 'reset'))
);
