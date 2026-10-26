-- 00012: scheduling tables (P4-B).
--
-- school_tasks: one row per (school, platform task) discovered from the
--   platform's signPosition payload. The pair is the unit a plan is built for.
-- window_cache: the discovered window per (school_task, day), with its source
--   (platform / cache / fallback) and a version for re-planning.
-- sign_tasks: the per-account attempt slot, unique per
--   (account, school_task, day) — the single-winner CAS key. One row per
--   account per task per business day, no exceptions.
-- egresses: known egress identities for the per-egress GCRA limiter.

-- +goose Up
CREATE TYPE sign_task_state AS ENUM ('pending', 'running', 'success', 'failed', 'skipped');

CREATE TABLE school_tasks (
    id         uuid PRIMARY KEY DEFAULT uuidv7(),
    school_id  uuid NOT NULL REFERENCES schools (id) ON DELETE RESTRICT,
    name       text NOT NULL,
    enabled    boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_school_tasks_school_name UNIQUE (school_id, name),
    CONSTRAINT ck_school_tasks_name_len CHECK (char_length(name) BETWEEN 1 AND 128)
);

CREATE TRIGGER trg_school_tasks_updated_at BEFORE UPDATE ON school_tasks
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE window_cache (
    id            uuid PRIMARY KEY DEFAULT uuidv7(),
    school_task_id uuid NOT NULL REFERENCES school_tasks (id) ON DELETE CASCADE,
    day           date NOT NULL,
    start_at      timestamptz NOT NULL,
    end_at        timestamptz NOT NULL,
    source        text NOT NULL,
    version       integer NOT NULL DEFAULT 1,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_window_cache_task_day UNIQUE (school_task_id, day),
    CONSTRAINT ck_window_cache_source CHECK (source IN ('platform', 'cache', 'fallback')),
    CONSTRAINT ck_window_cache_bounds CHECK (start_at < end_at)
);

CREATE TABLE sign_tasks (
    id             uuid PRIMARY KEY DEFAULT uuidv7(),
    account_id     uuid NOT NULL REFERENCES accounts (id) ON DELETE RESTRICT,
    school_task_id uuid NOT NULL REFERENCES school_tasks (id) ON DELETE RESTRICT,
    day            date NOT NULL,
    run_at         timestamptz NOT NULL,
    state          sign_task_state NOT NULL DEFAULT 'pending',
    attempts       integer NOT NULL DEFAULT 0,
    result         text NOT NULL DEFAULT '',
    claimed_at     timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_sign_tasks_account_task_day UNIQUE (account_id, school_task_id, day),
    CONSTRAINT ck_sign_tasks_attempts CHECK (attempts >= 0)
);

CREATE TRIGGER trg_sign_tasks_updated_at BEFORE UPDATE ON sign_tasks
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- I-7 库级门：同账号同时至多一条 running（验收 3「同账号不双登录」）。
-- 进程内串行是应用层优化；崩溃重启后进程内状态清零，门必须在库层。
CREATE UNIQUE INDEX uq_sign_tasks_one_running ON sign_tasks (account_id) WHERE state = 'running';

CREATE INDEX idx_sign_tasks_run_at ON sign_tasks (run_at) WHERE state = 'pending';

-- 启动回收的查询面（P4-E worker 启动时按 claimed_at 找孤儿 running）。
CREATE INDEX idx_sign_tasks_claimed ON sign_tasks (claimed_at) WHERE state = 'running';

CREATE TABLE egresses (
    id         uuid PRIMARY KEY DEFAULT uuidv7(),
    identity   text NOT NULL,
    enabled    boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_egresses_identity UNIQUE (identity)
);

-- +goose Down
DROP TABLE egresses;
DROP INDEX idx_sign_tasks_claimed;
DROP INDEX idx_sign_tasks_run_at;
DROP INDEX uq_sign_tasks_one_running;
DROP TABLE sign_tasks;
DROP TABLE window_cache;
DROP TABLE school_tasks;
DROP TYPE sign_task_state;
