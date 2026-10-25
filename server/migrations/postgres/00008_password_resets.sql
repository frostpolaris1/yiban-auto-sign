-- 00008_password_resets: one-time password reset tokens.
--
-- Decision 12.14. The table stores only the SHA-256 of the token. used_at is
-- the single-use mark; expires_at bounds the token lifetime.

-- +goose Up
CREATE TABLE password_resets (
    id         uuid PRIMARY KEY DEFAULT uuidv7(),
    user_id    uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    token_hash bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    used_at    timestamptz,
    CONSTRAINT uq_password_resets_token UNIQUE (token_hash),
    CONSTRAINT ck_password_resets_token_len CHECK (octet_length(token_hash) = 32),
    CONSTRAINT ck_password_resets_expiry CHECK (expires_at > created_at)
);
