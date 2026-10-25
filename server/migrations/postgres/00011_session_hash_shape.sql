-- +goose Up
-- ip_hash and user_agent_hash store HMAC-SHA256 digests: 64 lowercase hex or
-- NULL. The CHECK turns any raw value that bypassed the typed constructors
-- into a rejected insert instead of silently stored personal data.
ALTER TABLE sessions
    ADD CONSTRAINT ck_sessions_ip_hash_shape
    CHECK (ip_hash IS NULL OR ip_hash ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT ck_sessions_user_agent_hash_shape
    CHECK (user_agent_hash IS NULL OR user_agent_hash ~ '^[0-9a-f]{64}$');

-- +goose Down
ALTER TABLE sessions
    DROP CONSTRAINT IF EXISTS ck_sessions_user_agent_hash_shape,
    DROP CONSTRAINT IF EXISTS ck_sessions_ip_hash_shape;
