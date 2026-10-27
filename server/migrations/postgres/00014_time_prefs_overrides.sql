-- 00014: user time preferences and manual window overrides (P4-F).
--
-- time_prefs: one row per user. offset_min is a relative minute from the
--   effective window start; null means "no preference, use automatic spread".
--   The five-minute grain matches the plan slice span. Rows are derived state:
--   deleting a user removes the preference.
-- window_overrides: a master or admin replacement for one (task, day).
--   It wins over the platform window for that day. The reason is kept so the
--   decision stays reviewable after the fact.
--
-- Rollback: the Down section is reversible. Down relabels window_cache rows
--   with source 'override' to 'cache' before it narrows the constraint back to
--   three values. The window times stay. The next discovery overwrites the
--   row with its platform source, so the transient label is self-healing.

-- +goose Up
-- Manual overrides are copied into window_cache as source 'override',
-- so the cache constraint must accept the fourth source value.
ALTER TABLE window_cache DROP CONSTRAINT ck_window_cache_source;
ALTER TABLE window_cache ADD CONSTRAINT ck_window_cache_source
    CHECK (source IN ('platform', 'cache', 'fallback', 'override'));

CREATE TABLE time_prefs (
    user_id    uuid PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    offset_min integer,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_time_prefs_offset CHECK (
        offset_min IS NULL OR (offset_min >= 0 AND offset_min < 1440 AND offset_min % 5 = 0)
    )
);

CREATE TABLE window_overrides (
    id             uuid PRIMARY KEY DEFAULT uuidv7(),
    school_task_id uuid NOT NULL REFERENCES school_tasks (id) ON DELETE CASCADE,
    day            date NOT NULL,
    start_at       timestamptz NOT NULL,
    end_at         timestamptz NOT NULL,
    reason         text NOT NULL,
    created_by     uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_window_overrides_task_day UNIQUE (school_task_id, day),
    CONSTRAINT ck_window_overrides_bounds CHECK (start_at < end_at),
    CONSTRAINT ck_window_overrides_reason_len CHECK (char_length(reason) BETWEEN 1 AND 200)
);

-- +goose Down
DROP TABLE window_overrides;
DROP TABLE time_prefs;
-- The narrower constraint rejects 'override'. Relabel those rows first;
-- dropping them would discard the cached window, which is still valid.
UPDATE window_cache SET source = 'cache' WHERE source = 'override';
ALTER TABLE window_cache DROP CONSTRAINT ck_window_cache_source;
ALTER TABLE window_cache ADD CONSTRAINT ck_window_cache_source
    CHECK (source IN ('platform', 'cache', 'fallback'));
