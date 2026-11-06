-- 00027: window_overrides.creator 降级为可空的复核软引用。
--
-- created_by 原为 NOT NULL + ON DELETE RESTRICT：创建过覆盖的用户因此永远
-- 无法被保留期物理删除，而删除覆盖行又会连带丢掉仍按日生效的窗口决定。
-- 覆盖按 (school_task, day) 逐日生效，效力不随创建者存在与否变化；
-- created_by 只供事后复核。故放开 NOT NULL，外键改 ON DELETE SET NULL：
-- 用户物理删除时保留其创建的覆盖行，仅把创建者引用置空。
--
-- Governance: migrations are forward-only. There is no Down section.

-- +goose Up
ALTER TABLE window_overrides ALTER COLUMN created_by DROP NOT NULL;
ALTER TABLE window_overrides DROP CONSTRAINT window_overrides_created_by_fkey;
ALTER TABLE window_overrides ADD CONSTRAINT window_overrides_created_by_fkey
    FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE SET NULL;
