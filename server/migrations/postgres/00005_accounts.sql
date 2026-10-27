-- 00005_accounts: signing accounts.
--
-- owner_user_id is NOT NULL and ON DELETE RESTRICT: a user that still owns an
-- account cannot be physically deleted. A soft delete writes deleted_at and
-- deleted_by. Only active rows take part in signing.
--
-- Governance: migrations are forward-only. There is no Down section.

-- +goose Up
CREATE TYPE account_status AS ENUM (
    'pending',
    'active',
    'rejected',
    'paused_admin',
    'paused_user',
    'credential_error'
);

CREATE TABLE accounts (
    id            uuid PRIMARY KEY DEFAULT uuidv7(),
    school_id     uuid NOT NULL REFERENCES schools (id) ON DELETE RESTRICT,
    owner_user_id uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    phone         text NOT NULL,
    status        account_status NOT NULL DEFAULT 'pending',
    reject_reason text NOT NULL DEFAULT '',
    reviewed_at   timestamptz,
    reviewed_by   uuid REFERENCES users (id) ON DELETE SET NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    deleted_at    timestamptz,
    deleted_by    uuid REFERENCES users (id) ON DELETE SET NULL,
    CONSTRAINT uq_accounts_id_school UNIQUE (id, school_id),
    CONSTRAINT ck_accounts_phone_shape CHECK (phone ~ '^1[0-9]{10}$'),
    CONSTRAINT ck_accounts_reject_reason CHECK (
        status <> 'rejected' OR char_length(reject_reason) > 0
    ),
    CONSTRAINT ck_accounts_review_pair CHECK (
        (reviewed_at IS NULL) = (reviewed_by IS NULL)
    ),
    CONSTRAINT ck_accounts_deleted_by CHECK (
        deleted_at IS NOT NULL OR deleted_by IS NULL
    )
);

CREATE TRIGGER trg_accounts_updated_at BEFORE UPDATE ON accounts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
