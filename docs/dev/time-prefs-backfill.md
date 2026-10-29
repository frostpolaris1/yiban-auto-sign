# 旧自选时间片补录（time_prefs 一次性）

## 1. 用途

旧栈按手机号存自选时间片。新栈按用户存一条偏移。
导入器不搬这两行（见 `server/internal/adapters/legacy/doc.go` 的裁决节）。
本页给出一条一次性补录路径：导出 → 导入 → 补录 → 核对。

前提有两条：

1. 旧库已用 P8-A 导出器导出。
2. `migrate-legacy` 已跑完，`legacy_ids` 已有旧账号到新账号的映射。

补录靠 `legacy_ids` 找新账号，再取该账号的 `owner_user_id`。
没有映射的旧账号一律跳过并点名。

## 2. 导出旧数据

```bash
python3 scripts/db_export.py --out /var/lib/yiban/export
```

导出器全程只读。它不写旧库，也不迁移 schema。
补录只读其中两个文件：`accounts.jsonl` 与 `time_prefs.jsonl`。

## 3. 导入账号

```bash
yiban-cli migrate-legacy --dir /var/lib/yiban/export --out /var/lib/yiban/import \
  --school-id <school-uuid> --owner-user-id <user-uuid>
```

这一步写入 `legacy_ids`。补录依赖它。
命令的完整旗位与退出码见 `server/cmd/cli/migrate_legacy.go` 的包头注释。

## 4. 补录：先跑 dry-run

```bash
yiban-cli backfill-time-prefs --dir /var/lib/yiban/export
```

默认 dry-run：一次写调用都不发。
stdout 是一份 JSON 报告。stderr 只放错误。
退出码：0 报告已出；1 读库失败；2 用法或输入错误。

读库连接串取 `YIBAN_DATABASE_URL`。可用 `--dsn` 覆盖。
空输入也是一份正常报告：0 行，退出码 0。文件缺失才是退出码 2。

## 5. 核对报告

报告字段：

| 字段 | 含义 |
|------|------|
| `format` | 报告版本，`yiban-time-pref-backfill/1` |
| `apply` | 本次是否写库 |
| `input_rows` | `time_prefs.jsonl` 行数 |
| `accounts_read` | `accounts.jsonl` 行数 |
| `writes` | 将写/已写的条目：`user_id`、`offset_min`、来源掩码手机号 |
| `conflicts` | 取值冲突的用户：全部取值与来源掩码手机号 |
| `skips` | 被跳过的旧行：掩码手机号、原因、补充文本 |
| `rows_written` | 数据库实际变更行数；dry-run 恒为 0 |

报告零明文手机号。手机号一律保留前 3 后 4（`core/mask` 同一实现）。

跳过原因与处置：

| `reason` | 含义 | 处置 |
|----------|------|------|
| `missing_phone` | 旧行 `phone` 是空串 | 旧库数据缺陷，人工查看 |
| `slot_min_not_integer` | `slot_min` 缺失或不是整数 | 旧库数据缺陷，人工查看 |
| `slot_min_out_of_range` | 取值不在 `[0, 1440)` | 旧库数据缺陷，人工查看 |
| `slot_min_not_multiple_of_5` | 取值不是 5 的倍数 | 旧库数据缺陷，人工查看 |
| `duplicate_input_phone` | 同一手机号在导出物里出现多次：首行保留，后续行拒绝 | 导出物被改，重新导出 |
| `phone_without_account` | 旧库孤儿：`time_prefs.phone` 无账号行 | 默认跳过；导入器同口径计数 |
| `account_without_legacy_mapping` | 旧账号没有 `legacy_ids` 映射 | 该账号未导入，补录无法定位用户 |
| `account_or_user_inactive` | 落地账号或归属用户已软删 | 不写已删对象 |

`conflicts` 的口径：同一用户映射到多个旧账号，且取值不一致。
工具不合并、不猜测。整用户跳过。人按下表核对后手工决定。

## 6. 落库

```bash
yiban-cli backfill-time-prefs --dir /var/lib/yiban/export --apply
```

写入在单个事务内。任一写失败整批回滚，不留半写。
重复执行幂等：同值重跑零变更，`rows_written` 为 0。
已存在不同取值时旧值优先，`rows_written` 计为 1。

工具**不**跑迁移。库要先 `yiban-cli migrate`。
工具也**不**改调度与签到语义：它只写 `time_prefs` 一张表。

## 7. 核对

```sql
-- 行数应等于 dry-run 报告的 writes 条数。
SELECT count(*) FROM time_prefs;

-- 逐用户核对取值（user_id 用你自己的库值）。
SELECT user_id, offset_min, updated_at FROM time_prefs ORDER BY user_id;
```

再跑一次 dry-run。报告的 `writes` 应与落库前一致。
`conflicts` 与 `skips` 的条数应不变。

想看某用户的来源账号，用 `legacy_ids` 反查：

```sql
SELECT l.old_id, a.phone
FROM legacy_ids l JOIN accounts a ON a.id = l.new_id
WHERE l.old_table = 'accounts' AND a.owner_user_id = '<user-uuid>';
```

## 8. 口径

- 映射是 1:1：旧账号 → `legacy_ids` → 新账号 → `owner_user_id`。
- 同一用户多账号取值一致才写。不一致整用户跳过并点名。
- 取值边界取自 `core/schedule`：`0 <= offset < 1440`，且为 5 的倍数。
  建表 CHECK（`migrations/postgres/00014`）取同值。两侧不一致时数据库报错，不静默写入。
- 旧库按窗口裁剪的保存判据无法在补录时求值。补录只校验存储边界。
- 零新依赖：工具读导出物，不读 SQLite，不引 Go SQLite 驱动。
- 报告与错误文本零明文手机号。
