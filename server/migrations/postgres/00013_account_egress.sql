-- 00013: sticky account-to-egress binding (P4-D).
--
-- One egress per account, chosen once at first use. Egress failures do not
-- migrate bound accounts: the binding is the isolation boundary itself.
-- ON DELETE CASCADE on accounts: the binding is derived state, never history.

-- +goose Up
CREATE TABLE account_egress (
    account_id  uuid PRIMARY KEY REFERENCES accounts (id) ON DELETE CASCADE,
    egress_id   uuid NOT NULL REFERENCES egresses (id) ON DELETE RESTRICT,
    assigned_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_account_egress_egress ON account_egress (egress_id);

-- +goose Down
DROP INDEX idx_account_egress_egress;
DROP TABLE account_egress;
