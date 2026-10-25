-- 00007_audit_logs: append-only audit chain.
--
-- There are no foreign keys. A physical user delete would otherwise rewrite
-- audit rows, and audit history must not follow object deletion.
--
-- detail is text, not jsonb: jsonb does not guarantee a stable text form
-- across versions, which would produce false chain failures.

-- +goose Up
CREATE TABLE audit_logs (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts            timestamptz NOT NULL,
    school_id     uuid,
    actor_user_id uuid,
    actor_role    user_role,
    actor_label   text NOT NULL,
    action        text NOT NULL,
    target_type   text NOT NULL DEFAULT '',
    target_id     text NOT NULL DEFAULT '',
    detail        text NOT NULL DEFAULT '{}',
    prev_hash     bytea NOT NULL,
    hash          bytea NOT NULL,
    CONSTRAINT ck_audit_prev_len CHECK (octet_length(prev_hash) = 32),
    CONSTRAINT ck_audit_hash_len CHECK (octet_length(hash) = 32),
    CONSTRAINT ck_audit_action_shape CHECK (action ~ '^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$'),
    CONSTRAINT ck_audit_detail_len CHECK (octet_length(detail) <= 4096),
    CONSTRAINT ck_audit_actor_masked CHECK (
        position('@' in actor_label) = 0 OR position('***' in actor_label) > 0
    )
);

-- +goose StatementBegin
CREATE FUNCTION audit_logs_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'audit_logs is append-only; UPDATE is forbidden';
    END IF;
    IF coalesce(current_setting('yiban.audit_purge', true), '') <> 'on' THEN
        RAISE EXCEPTION 'audit_logs delete requires yiban.audit_purge=on';
    END IF;
    IF TG_LEVEL = 'ROW' THEN
        RETURN OLD;
    END IF;
    RETURN NULL;
END
$$;
-- +goose StatementEnd

CREATE TRIGGER trg_audit_logs_append_only
    BEFORE UPDATE OR DELETE ON audit_logs
    FOR EACH ROW EXECUTE FUNCTION audit_logs_guard();

CREATE TRIGGER trg_audit_logs_no_truncate
    BEFORE TRUNCATE ON audit_logs
    FOR EACH STATEMENT EXECUTE FUNCTION audit_logs_guard();
