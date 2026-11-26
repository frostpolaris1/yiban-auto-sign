-- +goose NO TRANSACTION

-- Governance: migrations are forward-only. There is no Down section.

-- P3-9/P3-11④：通知投递的认领租约。
--
-- 病根：投递调度只按到期时间查询 pending 行，任意实例都能取到同一批行。
-- 多实例（滚动发布、双副本、重启重叠）同时跑 SendDue 时，同一行会被并发
-- 重复投递；至少一次语义接受重试造成的重复，但并发这一档可以消除。
--
-- 做法：给投递账本加 claimed_until 租约列。调度方用一条
-- UPDATE ... FROM (SELECT ... FOR UPDATE SKIP LOCKED) 认领到期行，
-- 并写 claimed_until = now + lease（15 分钟，见 app/notify/delivery.go 的
-- deliveryLease）；同一租约内其它实例认领不到该行。实例在确认（Mark*）前
-- 崩溃时，租约到期后该行可被重新认领，仍保持至少一次投递。
--
-- 索引：认领谓词是 status='pending' AND next_retry_at <= now AND
-- (claimed_until IS NULL OR claimed_until <= now)。00026 的局部索引
-- idx_deliveries_due (next_retry_at) WHERE status='pending' 已能定位小批
-- 到期行，claimed_until 只是行级过滤，不另建索引。
--
-- 事务：单条可空列 ALTER，语句级原子完成；失败可重跑，IF NOT EXISTS
-- 让重跑幂等，故本文件 NO TRANSACTION。

-- +goose Up
ALTER TABLE notification_deliveries ADD COLUMN IF NOT EXISTS claimed_until timestamptz;
