-- 00016: per-account location policy (P5-C).
--
-- account_location: one row per account. Missing row means "auto": the
--   platform samples the position. mode fixed pins lng/lat and carries a
--   free-text reason. The reason travels to the platform when the fixed
--   point falls outside the target fence, so the operator can explain why.
-- lng/lat are nullable only for auto rows; the CHECK makes a fixed row
--   impossible without a complete point (fail-closed at the storage layer).
-- updated_by points at the operator who wrote the row; it is a soft
--   reference for review only, no ON DELETE action on purpose.

-- +goose Up
CREATE TABLE account_location (
    account_id uuid PRIMARY KEY REFERENCES accounts (id) ON DELETE CASCADE,
    mode       text NOT NULL,
    lng        double precision,
    lat        double precision,
    reason     text NOT NULL DEFAULT '',
    updated_by uuid REFERENCES users (id),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_account_location_mode CHECK (mode IN ('auto', 'fixed')),
    CONSTRAINT ck_account_location_fixed_point CHECK (
        mode <> 'fixed' OR (lng IS NOT NULL AND lat IS NOT NULL))
);

-- +goose Down
DROP TABLE account_location;
