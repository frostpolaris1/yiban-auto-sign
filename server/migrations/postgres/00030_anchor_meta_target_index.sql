-- 00030_anchor_meta_target_index: 库内锚点高水位与审计目标索引。
--
-- anchor_meta 是单行表：记录最后一次成功写入的锚点 seq 与链头哈希。
-- 锚点文件是 append-only；本行让巡检端点能检出"锚点文件末行被删"——
-- 只比文件内的行间关系时，删掉最后一行后剩余行仍自洽。
-- 表只允许一行（singleton 恒真），由 CLI 在锚点文件追加成功后覆盖写。
--
-- idx_audit_target_id_ts 服务 GET /api/audit-logs?target=...：
-- 按 target_id 精确过滤后再按 ts 倒序取页。
-- 00009 已有 (target_type, target_id, ts DESC)，只按 target_id 过滤用不上它的前导列；
-- 本索引补 target_id 单列前缀，名字避开既有 idx_audit_target_ts。
--
-- Governance: migrations are forward-only. There is no Down section.

-- +goose Up
CREATE TABLE anchor_meta (
    singleton  boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    seq        bigint NOT NULL CHECK (seq > 0),
    head_hash  bytea NOT NULL CHECK (octet_length(head_hash) = 32),
    updated_at timestamptz NOT NULL
);

CREATE INDEX idx_audit_target_id_ts ON audit_logs (target_id, ts DESC);
