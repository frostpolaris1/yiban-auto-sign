-- 00017_legacy_ids: old-to-new primary key map for the legacy importer (P8-B).
--
-- One row per imported legacy row. The importer is idempotent on this table:
-- a second pass finds every (old_table, old_id) and inserts nothing.
--
-- new_id is uuid: the new stack's users.id and accounts.id are uuidv7, not
-- integers. (The dispatch sketch said int; the actual key types are uuid.
-- Deviation reported to the dispatcher, 2026-10-27.)
--
-- Governance: migrations are forward-only. There is no Down section.

-- +goose Up
CREATE TABLE legacy_ids (
    old_table text NOT NULL,
    old_id    integer NOT NULL,
    new_id    uuid NOT NULL,
    PRIMARY KEY (old_table, old_id)
);
