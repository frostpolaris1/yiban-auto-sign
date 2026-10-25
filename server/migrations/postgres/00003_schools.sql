-- 00003_schools: schools.
--
-- A school is never deleted. Disabling writes status = 'disabled'. The column
-- is named time_zone to avoid shadowing the SQL function timezone(text,
-- timestamptz).

-- +goose Up
CREATE TYPE school_status AS ENUM ('active', 'disabled');

CREATE TABLE schools (
    id         uuid PRIMARY KEY DEFAULT uuidv7(),
    code       text NOT NULL,
    name       text NOT NULL,
    status     school_status NOT NULL DEFAULT 'active',
    time_zone  text NOT NULL DEFAULT 'Asia/Shanghai',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_schools_code UNIQUE (code),
    CONSTRAINT ck_schools_code_shape CHECK (code ~ '^[a-z0-9][a-z0-9-]{1,31}$'),
    CONSTRAINT ck_schools_name_len CHECK (char_length(name) BETWEEN 1 AND 128),
    CONSTRAINT ck_schools_time_zone CHECK (time_zone IN ('Asia/Shanghai'))
);

CREATE TRIGGER trg_schools_updated_at BEFORE UPDATE ON schools
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
