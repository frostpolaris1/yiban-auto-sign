-- 00004_admin_schools: administrator bindings.
--
-- Revoking a grant deletes the row. Scope resolution reads users.role first,
-- so a leftover binding on a demoted user has no effect.
--
-- Governance: migrations are forward-only. There is no Down section.

-- +goose Up
CREATE TABLE admin_schools (
    user_id    uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    school_id  uuid NOT NULL REFERENCES schools (id) ON DELETE CASCADE,
    granted_by uuid REFERENCES users (id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, school_id)
);
