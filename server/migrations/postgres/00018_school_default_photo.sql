-- 00018: school default photo reference (P5 remaining, g1u).
--
-- schools.default_photo_id registers the school's default photo. NULL means
--   no default photo is set. The image bytes stay in the storage adapter;
--   this column only carries the reference.
-- ON DELETE SET NULL: the retention sweep physically deletes photo rows.
--   A dangling reference must never survive it. Soft-deleted photos are
--   filtered at read time (the row still exists), so this action only fires
--   on a real purge.
-- No new table: the reference is one nullable column on a row that already
--   exists. schools is never deleted, so no other lifecycle is needed.

-- +goose Up
ALTER TABLE schools
    ADD COLUMN default_photo_id uuid REFERENCES photos (id) ON DELETE SET NULL;

-- +goose Down
ALTER TABLE schools DROP COLUMN default_photo_id;
