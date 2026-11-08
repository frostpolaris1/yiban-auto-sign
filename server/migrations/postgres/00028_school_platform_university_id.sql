-- 00028: schools.platform_university_id.
--
-- schools.platform_university_id stores the platform's university identifier
-- that master binds to a school row. The verify endpoint returns the identity
-- the platform sends with the issuer receipt; school matching compares that
-- university id against this column. Nullable: the binding is optional and
-- absent until master sets it. The partial unique index lets many schools
-- stay unbound while forbidding two schools to claim the same university.
-- Clearing the binding writes NULL, which the index ignores.
--
-- No foreign key: the id is a platform-side identifier, not a local key.
-- No length CHECK: the use case caps it at the contract's 128 characters;
-- the same invariant is kept in exactly one place.
--
-- Governance: migrations are forward-only, but this change is a plain column
-- drop, so a Down section keeps Up -> Down -> Up exercisable like 00018.

-- +goose Up
ALTER TABLE schools ADD COLUMN platform_university_id text;
CREATE UNIQUE INDEX uq_schools_platform_university_id
    ON schools (platform_university_id) WHERE platform_university_id IS NOT NULL;

-- +goose Down
DROP INDEX uq_schools_platform_university_id;
ALTER TABLE schools DROP COLUMN platform_university_id;
