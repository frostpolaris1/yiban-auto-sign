-- 00031_school_owned_photos: D6 乙案——学校默认图自有化（P3-17 修复批 R5）。
--
-- 病根（00018 形状）：schools.default_photo_id 直挂**属主用户行**，
-- 属主软删即默认漂移、重传同字节又复活、属主硬删 purge 级联带走引用行。
-- 乙案：设默认＝把内容寻址字节**复制登记**为一条 school-owned 行
-- （owner_user_id IS NULL、school_id 指向该学校；字节在存储侧按 sha256
-- 寻址，磁盘零增量）。此后属主删图、用户保留期清理、属主 purge 级联
-- 都只触 owner_user_id 行，school-owned 行按 owner 排除——级联查询天然
-- 不命中（FK CASCADE 只删 owner_user_id 引用行；PurgeExpired 本批加
-- owner_user_id IS NOT NULL 谓词，用户保留期不辖学校资产）。
--
-- Schema（最小形状，二选一互斥）：
--   owner_user_id 放开 NOT NULL；新增 school_id NULL REFERENCES schools
--   ON DELETE CASCADE（schools 设计上不删，动作只为完备）。
--   ck_photos_owner_or_school：两列恰好一个非空——用户行 owner 侧、
--   学校资产行 school 侧，没有第三种形状。
--   uq_photos_school_sha（partial, WHERE owner_user_id IS NULL）：
--   学校侧内容寻址幂等键；用户侧唯一键 uq_photos_owner_sha 里 owner
--   为 NULL 的行不参与比较，两族行互不串键——**重传同字节只复活用户
--   自己的行，不复活学校默认**（谓词 P3-17/P2-4 同族口径）。
--
-- 存量迁移：默认指针指向未软删用户行时，复制登记同字节的学校资产行并
-- 把指针改道到学校行；指针已悬（引用行已软删——读面本来就按未设置回）
-- 的不动，管理员下一次显式 PUT 自然重建。
-- 取消默认＝admin 显式 PUT null：应用层删指针＋学校资产行（内容寻址，
-- 行删字节留给 GC；GC 引用集合按行计，属主用户行若在则字节保留）。
--
-- Governance: migrations are forward-only. There is no Down section.

-- +goose Up
ALTER TABLE photos
    ALTER COLUMN owner_user_id DROP NOT NULL;

ALTER TABLE photos
    ADD COLUMN school_id uuid REFERENCES schools (id) ON DELETE CASCADE;

ALTER TABLE photos
    ADD CONSTRAINT ck_photos_owner_or_school CHECK (
        (owner_user_id IS NULL) <> (school_id IS NULL)
    );

CREATE UNIQUE INDEX uq_photos_school_sha ON photos (school_id, sha256)
    WHERE owner_user_id IS NULL;

-- 存量默认改道：仅「引用行未软删且属用户行」的形状复制登记并改指针；
-- 其余（缺指针/悬引用）保持原样，读面语义已正确。
WITH src AS (
    SELECT s.id AS school_id, p.sha256, p.mime, p.width, p.height, p.bytes, p.created_at
      FROM schools s
      JOIN photos p ON p.id = s.default_photo_id
     WHERE s.default_photo_id IS NOT NULL
       AND p.deleted_at IS NULL
       AND p.owner_user_id IS NOT NULL
), ins AS (
    INSERT INTO photos (owner_user_id, school_id, sha256, mime, width, height, bytes, created_at)
    SELECT NULL, school_id, sha256, mime, width, height, bytes, created_at FROM src
    ON CONFLICT (school_id, sha256) WHERE owner_user_id IS NULL DO UPDATE SET deleted_at = NULL
    RETURNING id, school_id
)
UPDATE schools s
   SET default_photo_id = ins.id
  FROM ins
 WHERE s.id = ins.school_id;
