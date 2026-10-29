-- 00019: 用户列表的键集分页索引。
--
-- GET /api/users 在不带过滤时按 (created_at DESC, id DESC) 取页。
-- 既有 idx_users_role_live 以 role 打头，无法服务无角色过滤的排序。
-- 索引只覆盖未删除行：列表与容量口径都按 deleted_at IS NULL。
--
-- Governance: migrations are forward-only. There is no Down section.

-- +goose Up
CREATE INDEX idx_users_created_live ON users (created_at DESC, id DESC)
    WHERE deleted_at IS NULL;
