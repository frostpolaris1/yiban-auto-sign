# 旧栈 SQLite → 新栈 PostgreSQL 导入

> 适用：Go 栈上线窗口的一次性数据导入。只导入用户与账号（含凭据）；
> 自选时间、签到任务、审计、照片、统计历史不导入。
> 本页只写流程与判据，不写生产计数，也不写任何用户/账号 ID、邮箱、手机号、
> 姓名或学校名；数量与标识一律用占位符，并以导出/导入的 `summary.json` 核对。

## 1. 前置条件

- 旧栈仍可启动，旧库文件可读；旧栈代码版本与库 schema 匹配。
- 目标 PostgreSQL 是独立的新库或一次性实例。不要在已有真实用户的库上执行；
  幂等重跑只针对同一批旧库导出物。
- 新库已迁移到最新：`yiban-cli migrate`。导入器自己也会应用迁移；先跑一次
  是为了在数据窗口前暴露迁移问题。
- 新 master 已创建。旧内置 master 不在旧 `users` 表里，不能沿用；
  新 master 必须用新邮箱与新口令单独创建。
- 学校已创建。旧库没有学校表；全部旧账号落同一个 `--school-id`。
  多学校迁移不在本流程范围。
- `--school-id` 与 `--fallback-owner-user-id` 必须已存在且可用；
  导入器不建学校、不建用户。
- 三个密钥只从进程环境读：`YIBAN_DATABASE_URL`（DSN）、
  `YIBAN_CREDENTIAL_KEY`（base64 的 32 字节）、`YIBAN_LEGACY_ACCOUNTS_KEY`
  （64 位 hex）。前两个与目标新库同源；最后一个只给本 CLI，
  不进 `/etc/yiban/server.env`。
- 备份目录、导出目录、报告目录都先建为 `0700`；文件为 `0600`。
  工具不会修正已存在目录的权限，必须预先建好。
- 以下命令以裸机 `/etc/yiban/server.env` 为例；容器形态把同一批
  `YIBAN_*` 注入 app 容器后执行等价命令。

### 1.1 旧库备份

```bash
install -d -m 0700 <备份目录>
# 旧栈仍在写时优先用 SQLite 在线备份；能停写就先停写再复制。
sqlite3 <旧库路径> ".backup '<备份文件>'"
chmod 600 <备份文件>
sha256sum <备份文件>
```

没有 `sqlite3` 时，先停旧栈写入，再复制旧库文件。备份含邮箱、手机号和
旧凭据密文，按秘密级保管。

### 1.2 新库迁移与 master

```bash
# 指向目标新库的运行环境；不要 source 旧栈的 .env。
set -a; . /etc/yiban/server.env; set +a
yiban-cli migrate

read -rs MASTER_PASSWORD
printf '%s\n' "$MASTER_PASSWORD" | yiban-cli user create-master \
  --email <新 master 邮箱> --password-stdin
```

`user create-master` 输出的 `id` 就是后面的 `<master-uuid>`。
master 已存在时不要重复创建；用 `GET /api/auth/me` 取当前 master 的 `id`。
新 master 邮箱不要与旧库任何未软删用户的邮箱相同：新库对活跃邮箱有唯一约束，
撞邮箱会让对应用户导入时行级报错。

### 1.3 学校

旧库没有学校表。用新 master 登录管理界面创建一所学校（接口是
`POST /api/schools`，仅 master 可写；请求体 `code` 与 `name` 必填），
把响应里的 `id` 记为 `<school-uuid>`。学校代码建后不可改。

## 2. 导出

```bash
install -d -m 0700 <导出目录>
python3 scripts/db_export.py --out <导出目录> --db <旧库路径>
```

- 导出器全程只读：不建表、不迁移、不写审计。
- 默认拒绝覆盖已有导出文件；确认要替换才加 `--force`；只看目标加 `--dry-run`。
- 产物七件：`users.jsonl`、`accounts.jsonl`、`time_prefs.jsonl`、`audit.jsonl`、
  `schools.jsonl`（恒为空）、`reconciliation.jsonl`、`summary.json`。
- `schools.jsonl` 非空会被导入器拒绝；旧库无学校表，正常导出必为空。
- 导出物不入仓库、不进 CI 日志或聊天记录；目录 `0700`，文件 `0600`。

导出后先看 `summary.json`：

| 字段 | 含义 | 处理 |
|---|---|---|
| `rows` | 四张表行数 | 作为导入 `rows_read` 的基线 |
| `orphans.time_prefs_phone_without_account` | 自选时间无对应账号 | `--strict` 下非零即拒绝导入；先查清再导 |
| `duplicates.users_by_email` / `accounts_by_phone` | 重复邮箱/手机号 | 非零先核对活跃重复；新库对未软删行有唯一约束 |
| `soft_deleted.users` / `accounts` | 软删行数 | 与导入后库内软删计数对拍 |
| `plaintext_resident` | 旧库明文/损坏凭据 | 非零表示导出器已置空并计数；被置空的是口令字段时，该账号导入后无凭据，须人工处置 |

## 3. 导入

```bash
# 目标新库环境；不是旧栈 .env。
set -a; . /etc/yiban/server.env; set +a
# 旧钥只进当前 shell 环境，不落 server.env，不进 argv。
read -rs YIBAN_LEGACY_ACCOUNTS_KEY && export YIBAN_LEGACY_ACCOUNTS_KEY

install -d -m 0700 <报告目录>
yiban-cli migrate-legacy \
  --dir <导出目录> \
  --out <报告目录> \
  --school-id <school-uuid> \
  --fallback-owner-user-id <master-uuid> \
  --admin-school-id <school-uuid> \
  --strict
```

- `--admin-school-id` 可省略，缺省等于 `--school-id`。只有旧 admin 要绑到
  另一所已存在的学校时才显式给。
- 不要用 `--dsn` 传生产 DSN：命令行参数会进 shell 历史与 `ps`。
  `YIBAN_DATABASE_URL` 与 `YIBAN_CREDENTIAL_KEY` 从环境文件读。
- `YIBAN_LEGACY_ACCOUNTS_KEY` 必须是 64 位 hex（32 字节）。
- `--strict` 在存在孤儿 `time_prefs` 时拒绝导入；生产窗口应使用它。
- 导入顺序固定：先用户，后账号；账号 owner 逐行解析。
  每行的主行、`legacy_ids` 映射与凭据在同一个保存点内同存亡。

退出码与写入边界：

| 退出码 | 场景 | 写入 |
|---|---|---|
| `0` | 成功；`--strict` 下无孤儿、无行错误 | 已提交 |
| `1` | `--strict` 有孤儿：事务开始前失败 | 零写入 |
| `1` | 行级错误：好行已提交，坏行未提交 | 部分提交 |
| `2` | 用法、学校/兜底用户、导出物或对账校验失败 | 事务回滚，零写入 |

报告写入失败也返回 `2`，但导入事务已经提交。遇到这种 `2` 先查
`legacy_ids` 与 `summary.json`，再决定是否重跑；重跑是幂等的。
不要手改导出物：无行错误时，导入器在提交前逐行比对
`reconciliation.jsonl`，不一致即回滚。

## 4. 对账

导入的 stdout 打印 summary JSON，随后一行完成提示；同一份 summary 落
`<报告目录>/summary.json`。对账表落 `<报告目录>/reconciliation.jsonl`。

| 核对项 | 看哪里 | 通过条件 |
|---|---|---|
| 输入行数 | 导出 `summary.json.rows` 与导入 `rows_read` | 四张表逐项相等 |
| 落库守恒 | `rows_read`、`rows_imported`、`already_imported`、`row_errors` | 每表相加相等 |
| 用户/账号 | `rows_imported` | 首导为全量；重跑为 0 |
| 归属 | `owner_resolution`、`fallback_owner_rows` | `fallback_owner_rows` 等于 `owner_resolution.fallback`；`unresolved` 与 `ambiguous` 为 0，否则逐条看行错误 |
| 角色 | `role_mapping` | 首导 `admin->admin` 与 `user->user` 计数之和等于 `rows_imported.users`；重跑为空是正常 |
| 旧 admin 绑定 | `admin_schools` | `bound` 等于旧 admin 行数；首导 `created` 等于 `bound`，重跑为 0 |
| 凭据 | `credentials` | 首导：`decrypt_failed` 为 0；`created + missing` 覆盖已导入账号；`phone_code_dropped` 只作计数；库内凭据行数等于 `created`。重跑：三项为 0，库内行数不变 |
| 软删 | 导出 `soft_deleted` 与库内计数 | 用户/账号分别相等 |
| 备注名 | 库内 `name <> ''` 与导出 `accounts.jsonl` | 计数相等；空名允许 |
| 映射 | `legacy_ids` | 用户与账号映射数等于已落地行数；无悬空 `new_id` |
| 对账表 | 退出码与 summary | 退出码 0 且 summary 中没有 `recon_compare`；出现 `not compared: row errors present` 表示有行错误，比对未执行 |

以下查询只回计数与枚举值，不选邮箱、手机号或姓名。连接参数用 `PG*`
环境或 `~/.pg_service.conf`，不要把 DSN 放进命令行。

```sql
-- 每张旧表的映射数。首次导入后：users + accounts 等于落地行数。
SELECT old_table, count(*) FROM legacy_ids GROUP BY old_table ORDER BY old_table;

-- 角色分布（只数旧库导入的用户）。
SELECT u.role, count(*)
FROM users u JOIN legacy_ids l ON l.old_table = 'users' AND l.new_id = u.id
GROUP BY u.role ORDER BY u.role;

-- 软删计数。
SELECT 'users' AS kind, count(*)
FROM users u JOIN legacy_ids l ON l.old_table = 'users' AND l.new_id = u.id
WHERE u.deleted_at IS NOT NULL
UNION ALL
SELECT 'accounts', count(*)
FROM accounts a JOIN legacy_ids l ON l.old_table = 'accounts' AND l.new_id = a.id
WHERE a.deleted_at IS NOT NULL;

-- 凭据行数。
SELECT count(*)
FROM credentials c JOIN legacy_ids l
  ON l.old_table = 'accounts' AND l.new_id = c.account_id;

-- 非空备注名计数。
SELECT count(*)
FROM accounts a JOIN legacy_ids l
  ON l.old_table = 'accounts' AND l.new_id = a.id
WHERE a.name <> '';

-- 悬空映射，必须为 0。
SELECT count(*) FROM legacy_ids l
WHERE (l.old_table = 'users'
       AND NOT EXISTS (SELECT 1 FROM users u WHERE u.id = l.new_id))
   OR (l.old_table = 'accounts'
       AND NOT EXISTS (SELECT 1 FROM accounts a WHERE a.id = l.new_id));
```

导出侧的备注名与凭据期望值：

```bash
python3 -c 'import json,sys; rows=[json.loads(x) for x in open(sys.argv[1], encoding="utf-8") if x.strip()]; print("name_nonempty", sum(1 for r in rows if r.get("name"))); print("password_envelope", sum(1 for r in rows if isinstance(r.get("password"), dict)))' <导出目录>/accounts.jsonl
```

`password_envelope` 是旧口令信封的行数，首导且 `decrypt_failed` 为 0 时
应等于 `credentials.created`；旧口令为空串的账号计 `credentials.missing`。
两者之和应覆盖已导入账号（前提是导出物没有被改出裸串）。

## 5. 幂等重跑

同一条 `migrate-legacy` 命令再跑一遍。全部已导入且无行错误时，预期：

- 退出码 0；`rows_imported` 全为 0，`already_imported` 等于 `rows_read`。
- `admin_schools.created` 为 0；`bound` 仍等于旧 admin 行数。
- `credentials` 三项全为 0；`owner_resolution` 全为 0。
- `legacy_ids` 不新增行；`reconciliation.jsonl` 与首次一致。

重跑不会更新已导入行。命中 `legacy_ids` 即跳过，不会用新导出物覆盖邮箱、
角色、备注名或凭据。若旧库在首次导入后改过数据，只能在新库重导，
不能靠重跑同步。

行级错误批次重跑会再次尝试未落地行；仍失败则再报错。修数据后重新导出，
再在新库重跑；已落地好行不会重复。

## 6. 登录冒烟

```bash
curl -fsS https://<域名>/healthz
```

用新 master 登录管理界面，确认：

- `GET /api/auth/me` 返回 master；`GET /api/schools` 有目标学校。
- 账号列表能看到导入的未软删账号；总数等于导入账号数减去导出
  `soft_deleted.accounts`。
- 抽查一个旧库 `phone_model` 非空的账号，详情显示同一机型。
  能显示说明新 KEK 解密成功；凭据缺失或解密失败时该字段为空。
- 普通用户用旧口令登录。成功后新栈把旧 scrypt 哈希当场重哈希为 argon2id，
  不需要重置口令。

如需验证平台口令，可用 `POST /api/accounts/{id}/verify`。它会真实登录平台，
受平台风控与限流约束；只抽 1 个账号，不要批量跑。

## 7. 回滚

- 切换前：旧栈不动。导入失败或对账不过，停新栈、保留旧栈；
  新库可整库丢弃后重导。退出码 2 是零写入；退出码 1 可能已部分提交，
  最稳的处理是丢弃新库重来。
- 切换后：反向代理切回旧栈，启动旧服务。旧库在保留期内可读。
  切换后在新栈产生的写入不会回旧库，回滚会丢这段数据。
- 丢弃命令只对一次性/新库实例执行，先确认库名不是旧库或生产库：

```bash
# 仅在一次性/新库实例执行；确认 <新库名> 是本次导入库。
dropdb --if-exists <新库名>
```

- 旧库与备份至少保留两周；确认没有回滚需求后，再按数据保管制度销毁。

## 8. 生产窗口步骤

1. T-1：在一次性/隔离 PG 上彩排；准备目标新库、master 与学校；
   核对三个密钥形状与旧库备份；发维护通知。
2. T0：停旧栈写入（停服务或进维护模式），确认不再有写。
3. T0：备份旧库，`chmod 600`，记录 sha256。
4. T0：从冻结后的旧库导出到 `0700` 目录，记录导出 `summary.json`。
5. T0：`migrate-legacy ... --strict`，记录退出码、stdout 与 stderr。
   需要留 stderr 时重定向到 `0600` 文件。
6. T0：按第 4 节对账。任何一项不符即停止，不切流量；需要回退时恢复旧栈服务。
7. T0：按第 6 节冒烟。
8. T0：反向代理切到新栈，启动新服务，观察 `/healthz` 与错误日志。
9. T0：旧栈保持停机，旧库与备份保留；不要在旧库上继续写。
10. T+1：观察登录失败、凭据解密失败与通知投递；提醒用户补全自选时间、
    照片与备注名，管理员按需重建学校任务。

窗口内不要做：改旧库 schema、在旧库上跑迁移或清理、把导出物放进仓库、
`work/`、CI 或聊天记录、把 DSN/密钥写进 argv 或日志。

## 9. 数据保管与销毁

- 旧库备份与导出物含邮箱、手机号与凭据密文；导入报告含旧行 ID 与哈希。
  三类都按秘密级保管：目录 `0700`、文件 `0600`、磁盘加密；离机副本先加密。
- 不进仓库、`work/`、CI 产物、工单附件或聊天记录。
- 旧钥只在导入操作员的进程环境或密钥库中；新 KEK 只在目标新栈的运行环境
  （裸机 `server.env` 或容器运行配置）。两者都不进 argv、日志或报告。
- 导入结束立即 `unset YIBAN_LEGACY_ACCOUNTS_KEY`；需要重跑时再从密钥库注入。
- 导入核对完成且保留期结束后：销毁导出物与报告；旧库与旧钥按同一保留期
  一起销毁。云快照、临时目录与操作机副本一并清理。
- 只记录谁在何时执行了导入，不记录导出内容。

## 10. 口径与限制

- owner 解析顺序固定：先 `lower(trim(email))` 精确命中旧用户，再按邮箱
  `@` 前缀命中；都未命中时，只有旧 `status=active` 的账号落
  `--fallback-owner-user-id`。非 active 账号解析不到按行错误处理。
- 邮箱形状的 owner 未精确命中时**不**退到前缀；前缀命中多个旧用户即歧义，
  按行错误处理，不按行序猜。
- 旧 admin 导入为 `admin`，并写一条 `admin_schools` 绑定；学校取
  `--admin-school-id`，缺省同 `--school-id`。重跑用 `ON CONFLICT DO NOTHING`
  保持幂等。
- 旧内置 master 不在旧 `users` 表，导入器不导入、不伪造该身份。它名下的
  active 账号按 owner 解析不到，落 `--fallback-owner-user-id`（通常是新 master）。
- 凭据：旧信封是 AES-256-GCM，AAD 为手机号；用旧钥解密后，用新 KEK
  按 `school_id|account_id` 的 AAD 重封装。新负载的 `device_code` 置空串；
  旧 `phone_code` 不迁移（新线通过 device-refresh 重新获取），只计数；
  `phone_model` 超过 64 字符按 rune 截断。
- 旧口令为空或解密失败：账号照建，凭据行缺省，分别计
  `credentials.missing` / `credentials.decrypt_failed`。导出物里的裸串是
  契约违约，该行整行回滚并计行错误。
- 备注名：旧 `accounts.name` 原样写入，空串允许；新表上限 50 字符，
  超长行会行级报错。旧 `sort_order` 在新 schema 无对应列，弃置。
- 状态：`pending`/`active`/`rejected` 保留；`active` 且 `user_paused=1`
  写为 `paused_user`；`deleted=1` 同时写 `deleted_at`；`reject_reason` 随行平移。
- 用户：邮箱转小写并去首尾空白；角色只接受 `admin`/`user`；`deleted_at`
  保留；`mail_notify` 保留旧 opt-out（缺键按开启）；`password_hash` 原样复制，
  登录成功后重哈希为 argon2id。邮箱大小写不同的活跃重复也会撞新库唯一索引。
- 账号手机号必须符合 11 位大陆手机形状；旧行不符合会行级报错。
- 旧 `rejected` 行必须带非空 `reject_reason`；缺失会触发新表 CHECK，
  行级报错。
- 时间：用户 `created_at` 保留旧值；旧库无时区的朴素时间按 `Asia/Shanghai`
  解读后转 UTC；空值用导入时刻。账号 `created_at` 不迁移，写导入时刻。
- 不导入：`time_prefs`、`audit`、学校表、照片、签到任务与统计历史。
  `--strict` 下孤儿 `time_prefs` 直接拒绝；旧审计证据留在导出物
  `audit.jsonl`，不续接新审计链。
- 旧自选时间片需要补录时，见
  [`docs/dev/time-prefs-backfill.md`](../dev/time-prefs-backfill.md)。
- 全部账号挂 `--school-id`；本流程不做多学校分配。旧库没有学校表，
  `schools.jsonl` 必须为空。
- summary 与行级错误只出计数、掩码手机号或掩码邮箱；完整邮箱、手机号、
  姓名与 owner 原文不落报告、不落日志。
