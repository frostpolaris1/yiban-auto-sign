-- 00015: photo library and per-user retention (P5-B).
--
-- photos: one row per (owner, preprocessed sha256). The unique key is the
--   dedupe key: re-uploading the same bytes restores a soft-deleted row
--   instead of creating a second one. sha256 is the blob key; the bytes
--   live in the storage adapter, never in PostgreSQL.
-- photo_retention: one row per user. Missing row means the default in
--   core/photo.DefaultRetentionDays; the closed set lives in
--   core/photo.RetentionDays and is mirrored by ck_photo_retention_days.

-- +goose Up
CREATE TABLE photos (
    id            uuid PRIMARY KEY DEFAULT uuidv7(),
    owner_user_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    sha256        bytea NOT NULL,
    mime          text NOT NULL,
    width         integer NOT NULL,
    height        integer NOT NULL,
    bytes         bigint NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    deleted_at    timestamptz,
    CONSTRAINT uq_photos_owner_sha UNIQUE (owner_user_id, sha256),
    CONSTRAINT ck_photos_sha_len CHECK (octet_length(sha256) = 32),
    CONSTRAINT ck_photos_mime CHECK (mime IN ('image/jpeg', 'image/png')),
    CONSTRAINT ck_photos_size CHECK (width > 0 AND height > 0 AND bytes > 0)
);

CREATE INDEX idx_photos_owner_created ON photos (owner_user_id, created_at DESC, id DESC)
    WHERE deleted_at IS NULL;

CREATE INDEX idx_photos_created ON photos (created_at)
    WHERE deleted_at IS NULL;

CREATE TABLE photo_retention (
    user_id    uuid PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    days       integer NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_photo_retention_days CHECK (days IN (30, 90, 180, 365))
);

-- +goose Down
DROP TABLE photo_retention;
DROP INDEX idx_photos_created;
DROP INDEX idx_photos_owner_created;
DROP TABLE photos;
