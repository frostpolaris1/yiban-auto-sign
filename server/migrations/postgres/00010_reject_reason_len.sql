-- +goose Up
-- The audit layer caps a reason at 200 characters (core/audit
-- MaxReasonChars). The API contract and this column use the same limit, so a
-- reason that the contract accepts cannot fail later in the audit write.
ALTER TABLE accounts
    ADD CONSTRAINT ck_accounts_reject_reason_len
    CHECK (reject_reason IS NULL OR char_length(reject_reason) <= 200);

-- +goose Down
ALTER TABLE accounts DROP CONSTRAINT IF EXISTS ck_accounts_reject_reason_len;
