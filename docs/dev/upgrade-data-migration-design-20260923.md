# 生产升级与数据迁移设计（v0.4.7 → v3 分支，2026-09-23）

> **是什么**：把生产库（`user_version=17`）升级到本分支（顶 `v20`）的**数据保全设计**——逐迁移的 SQL 与数据影响、继承的项目数据纪律、命令级执行序、对账验收门、回滚路径，以及逐行审查可逐条核对的判据。
> **状态**：定稿（待 T7 实现落地后回填 §5 的实测列）
> **范围**：只覆盖「库从 17 升到 20 且数据不丢不错」；不含 v3 的功能设计（见 `ledger-dual-version-design-20260923.md`）与任务划分（见 `m1-task-rearrangement-20260923.md`）。
> **读者**：本批的实施者与评审者必读；执行生产升级的运维必读；M2 审查者必读。
> **基线**：代码 `feature/backend-scheduler-v3 @ 32aab79`；生产 `server-web @ 6d4eafa`（v0.4.7）

**关键词**：升级、数据迁移、user_version 17→20、v18 平移、v19 epoch、v20 backfill、只增不改、备份门、回滚、对账、I-8、migrate=False

## 速览

- **生产实测**：`user_version=17`，10 张表，`accounts=90` / `users=111` / `sign_claims=309`（全为 `done`，09-17~09-23）；**无** `sign_tasks`/`egress_state`。
- **升级 = 3 步纯增量**：v18 建 2 张新表 + 把 309 行 `sign_claims` 平移进 `sign_tasks`；v19 给两表各加 `epoch` 列（`NOT NULL DEFAULT 0`）；v20 从 `sign-state-*.json` 补回 JSON 终态。**既有 10 张表零结构变更、零行数变更**（§5 的 A2 是**行数**判据；内容层面的既有回填不在此判据内，例如 `migrate_v6` 会把 `sign_events.account_id` 的 NULL 回填为 1）。
- **平移行是惰性的**：v18 给平移行设 `vshard=-1`，与任何执行体的分片集合不相交 ⇒ 永不被领取、永不被窃取，只作历史台账。
- **升级行为惰性**：`YIBAN_SCHEDULER_V3` 缺省 `0`，v3 代码路径在生产不执行 ⇒ 升级后 v2 行为逐字不变。
- **回滚 = 换回旧代码，库不动**：依据是「只增不改」+ `_run_migrations` 的 `version >= target_version: continue`（`migrations.py:846-848`）。先例：v0.4.4 部署记录同口径。
- **两条纪律是硬门**：升级前**当日备份包必须存在**（I-6）；**已发布**迁移不可再改（I-8）——**但 v18/v19 尚未发布，故仍可修改**（见 D3）。

## 目录

1. 生产现状与目标态
2. 升级动作清单（逐迁移的 SQL 与数据影响）
3. 继承的项目数据纪律
4. 升级执行序（runbook）
5. 数据保全与对账（验收门）
6. 回滚设计
7. 首次发布前必须收口的小项
8. 逐行审查对照清单

---

## 1. 生产现状与目标态

### 1.1 生产现状（2026-09-23 只读实测）

```bash
ssh yiban 'sqlite3 -readonly /opt/yiban-auto-sign/yiban.db "PRAGMA user_version;"'
ssh yiban 'sqlite3 -readonly /opt/yiban-auto-sign/yiban.db ".tables"'
```

| 项 | 实测值 | 说明 |
|----|--------|------|
| 部署版本 | `server-web @ 6d4eafa`，v0.4.7 | 生产迁移顶 = **v17**（`grep -n "^    (1[0-9]," yiban/store/migrations.py` 在服务器上止于 `(17, "v17_sign_claims")`） |
| `user_version` | **17** | 本分支顶为 **v20**（v18/v19 已实现于 `migrations.py:707/791`，v20 见 §2.3） |
| 表（10 张） | `accounts` `app_meta` `audit_logs` `session_cache` `sign_claims` `sign_events` `time_prefs` `user_delete_requests` `users` `verify_jobs` | **无** `sign_tasks` / `egress_state` |
| `accounts` | 90 行 | 巡检时（09-23 晨）89，仍在增长 |
| `users` | 111 行 | |
| `sign_claims` | **309 行，`state` 全为 `done`**，`day` ∈ 2026-09-17 ~ 2026-09-23 | v2 领取池；无 `claimed`/`failed` 残行 |
| `time_prefs` | 31 行 | 自选时间片偏好（事实源，非台账） |
| `app_meta` | 8 键 | 含 `audit_anchor_last` / `audit_anchor_meta`（审计链外部锚点指纹，**不可重建**） |

### 1.2 目标态

| 项 | 升级后 | 依据 |
|----|--------|------|
| `user_version` | 20 | §2 三步迁移 |
| 新表 | `sign_tasks`（v18）、`egress_state`（v18） | `migrate_v18` 的两段 DDL（见 §2.1；不引行号，随改动漂移）（`executor_heartbeats` 已按 R-T5 裁决**不建**：文件心跳已覆盖该职责） |
| 既有表结构 | **仅** `sign_claims` 增一列 `epoch INTEGER NOT NULL DEFAULT 0` | `migrations.py:805`；`ALTER TABLE ADD COLUMN` 带常量默认值在 SQLite 下是元数据级改动，不重写数据 |
| 既有表行数 | **逐表不变** | v18 对 `sign_claims` 只读（`INSERT OR IGNORE … SELECT`），v20 只写 `sign_tasks` |
| 行为 | v2 路径逐字不变（`YIBAN_SCHEDULER_V3` 缺省 0） | `m1-task-rearrangement-20260923.md:120` |

## 2. 升级动作清单（逐迁移的 SQL 与数据影响）

三步全部是**可选迁移**（`is_core=False`），语义见 §3.3。

### 2.1 v18 —— 建 2 张新表 + 平移领取池

```sql
CREATE TABLE IF NOT EXISTS sign_tasks (
  phone TEXT NOT NULL, day TEXT NOT NULL, vshard INTEGER NOT NULL,
  owner TEXT NOT NULL DEFAULT '', run_at TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 5, state TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0, lease_until TEXT NOT NULL DEFAULT '',
  epoch INTEGER NOT NULL DEFAULT 0, result TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, PRIMARY KEY (phone, day));
CREATE INDEX IF NOT EXISTS idx_tasks_pickup ON sign_tasks(day, vshard, state, run_at);
CREATE INDEX IF NOT EXISTS idx_tasks_lease  ON sign_tasks(state, lease_until);

INSERT OR IGNORE INTO sign_tasks
  (phone, day, vshard, owner, run_at, priority, state, attempts, lease_until, result, created_at)
SELECT phone, day, -1, owner, claimed_at, 5, state, attempts, heartbeat_at, result, claimed_at
  FROM sign_claims;
```

**数据影响**（`migrations.py:777-781`）：

- 建表/索引：`IF NOT EXISTS`，对既有数据零影响。
- 平移：**无 `WHERE`** ⇒ 生产 309 行全部复制为 309 行 `sign_tasks`；`sign_claims` **只被读，不被改**。
- `vshard=-1`、`run_at=claimed_at`、`lease_until=heartbeat_at`、`priority=5`。
- **惰性保证**：`vshard=-1` 与任何执行体的分片集合（`hrw.shards_of` 产出 `0..V-1`）不相交 ⇒ 平移行既不会被 `claim_batch` 领取，也不会被分片级窃取接管。**这是"升级不会导致重复真实登录"的关键性质**，必须由测试钉住（§8 第 6 条）。
- 幂等：`INSERT OR IGNORE` + `PK(phone,day)` ⇒ 重跑全跳过。
- 已知边角：若升级时恰好有 `state='claimed'` 的在飞行，其 `lease_until` 会是过去时间（`heartbeat_at`）。因惰性保证，该行仍不可被领取/窃取，只作历史记录。**运维要求：升级安排在签到窗口之外**（§4 步骤 1）。

### 2.2 v19 —— fencing epoch 列

```sql
ALTER TABLE sign_claims ADD COLUMN epoch INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sign_claims ADD COLUMN epoch INTEGER NOT NULL DEFAULT 0;  -- 经 _ensure_column，缺列才加
ALTER TABLE sign_tasks  ADD COLUMN epoch INTEGER NOT NULL DEFAULT 0;  -- v18 已建齐，此处兜底
```

**数据影响**（`migrations.py:791-807`）：既有行取默认值 `0`，**行数不变、其余列不变**。`NOT NULL DEFAULT 0` 是刻意的——`NULL` 会让 fencing 的算术静默变 `NULL`、守卫全部失效（`migrations.py:799-800`）。

### 2.3 v20 —— backfill：把 JSON 终态补回 `sign_tasks`（T7 待实现）

**为什么需要**：v18 的平移源是 `sign_claims`，而 `sign_claims` 只记**真实领取过**的账号（唯一写入点是 `claims.try_claim`），JSON 里的跳过类终态（`paused`/`user_cancelled`/`skipped_window` 等）从未进入 `sign_claims` ⇒ 只做 v18 的话，台账对"已跳过"的账号是空白的。

```
for day in 最近 14 天（参数化）:
    path = <YIBAN_STATE_DIR>/sign-state-<day>.json      # 只读这一个文件
    rows = json.load(path, encoding="utf-8-sig")         # BOM 容错
    for phone, entry in rows:
        st = from_status_json(entry["status"])           # 终态才 fold（DONE/SKIPPED/FAILED）
        INSERT OR IGNORE INTO sign_tasks (...) VALUES (phone, day, -1, 'backfill', <day> <time>, 5, st, 0, '', '', <day> <time>)
```

**数据影响**：

- 输入**只有** `sign-state-<day>.json`；`sign-daily-<day>.json` 是 `{phone: 符号}` 的**符号表**（`runner.py:538-560`），不含 `status`/`time`，**不得**作输入（`ledger-dual-version-design-20260923.md` D7）。
- 该文件缺失的日**跳过**（台账以两表为准），不报错。
- `vshard=-1`、`owner='backfill'` ⇒ 同 §2.1 的惰性保证。
- 幂等：`INSERT OR IGNORE` + `PK(phone,day)`；单事务 50 行/批；崩溃重跑收敛。
- **不覆盖**已有行（含 v18 平移行）——`OR IGNORE` 保证。

### 2.4 与 A 案（`D:/Documents/QQ/yiban-10k-scheduler-design.md` §8）迁移编号的对账

A 案 §8 给了一张迁移表，与本分支实际落地的编号**不一致**。**编号以本分支已实现的为准**（理由见 §3.2：迁移号一旦上生产即冻结，重号会改写已登记项）：

| A 案 §8 计划 | 本分支实际 | 处置 |
|---|---|---|
| v18 建 `sign_tasks`/`executor_heartbeats`/`egress_state` + `sign_claims` 平移（可选，落在 A 案的 `queue.db`） | `migrate_v18`，同内容，但表落在 **`yiban.db`** | 语义一致；库位置差异属**推迟**的三库分离（A 案 §7，合并文档 §2 #14 采纳但补"不假设跨库原子"）；本分支**不建** `executor_heartbeats`（见 §7.1） |
| v19 `sign_events` 迁 `events.db` + 旧表改名 `sign_events_legacy`（可选） | `migrate_v19` = **`fencing_epoch`**（两表加 `epoch`，B 案硬修正，合并文档 §2 #5） | **编号冲突**：events 迁库须**顺延（≥ v21）**，且属推迟的拆库批 |
| v20 `accounts.vshard_hint`（**核心**迁移） | v20 = backfill JSON 终态（可选，T7 实现） | **编号冲突**：`vshard_hint` 须**顺延（≥ v22）**；该列尚未纳入合并计划，登记待办 |
| v21 冻结旧表 | 未做 | 属退役批 |

**顺延规则（实施者必守）**：A 案中尚未实现的三项（events 迁库、`vshard_hint`、旧表冻结）**一律不得占用 v19/v20**，实施时从 **v21** 起顺延编号。

另有两处 A 案与本批的**有意分歧**（依据合并文档 §5「架构取 A、约束与证据取 B」）：

1. **灰度期判据**：A 案 §8 要求"双跑期两路径**共用 `sign_tasks` 的 state** 判当日了结"、"`sign_tasks` 与 `sign_claims` **双向同步**一个版本周期"。本批**不做双向同步**——SQLite 跨库事务不保证原子（合并文档 §3②），改用**每日单写者**：同一部署当日只有一个写者（由 `YIBAN_SCHEDULER_V3` 推出），**切换必须在当日首轮之前完成**。这是**运维硬规则**，代码暂不强制（`ledger_days` 已推迟，见 `m1-task-rearrangement-20260923.md` §8）。
2. **开关**：A 案 §11.4 另有 `YIBAN_TASK_QUEUE`（按账号数自动：N<500→0，N≥500→1）。本批只保留 `YIBAN_SCHEDULER_V3`（缺省 0，显式开启）；生产 N=90 落在自动档的 0 侧，两者当前不冲突。

另：A 案 §5.1（`:255-258`）对补签轮的口径是「web 日历、**补签轮判定**、容量统计全部**零改动平移**」、§8（`:461`）「补签轮、退出码契约（0/1/2/3/10）**逐字保留**」，并由 §5.3 的 `attempts` 落库跨执行体共享 + 退避降低"需要补签"的概率。即：A 案不把 07:12 撞锁当独立问题，而是让持久化队列**消掉它的成因**（补签＝再消费一遍队列，不再整轮重跑并长时间持锁）。本批沿用该口径，并把「日终封账」的消歧留给台账（`ledger-dual-version-design-20260923.md`，已推迟）。



## 3. 继承的项目数据纪律

本节逐条给既有文档/代码证据，本设计不得与之冲突。

| # | 纪律 | 原文依据 |
|---|------|---------|
| 3.1 | **迁移只增不改**（唯一先例口径） | `migrations.py:156`「迁移只增不改。」；`docs/refactor/45-backend-design-spec.md:561`（I-8）「已发布迁移不可修改；迁移只增不改；`user_version` 单调递增」 |
| 3.2 | **已发布迁移冻结**（按版本号的时间序列） | `migrations.py:21-22`「已发布的迁移函数不可再改（改了对已升级的库无效，还会给介于两个版本之间的库制造新的失败路径）」；`45-backend-design-spec.md:639`「`migrate_v4`/`migrate_v6` 一字不改（不变量 I-8）」 |
| 3.3 | **核心/可选迁移的失败语义** | `migrations.py:836-843`：核心失败抛异常、`init_db` 阻断启动；可选失败/延后只置 `blocked` 并 `continue`，**blocked 期间任何迁移都不提升 `user_version`**，下次启动重试。每个迁移全程包 `BEGIN IMMEDIATE`（`:850-858`）⇒ 中间态对外不可见 |
| 3.4 | **备份先行**（动迁移的前置条件） | `ledger-dual-version-design-20260923.md` I-6 / §3.1 Phase 0：当日 `/var/backups/yiban-<date>.tar.gz.gpg` 存在，否则拒绝执行迁移步并告警 |
| 3.5 | **备份怎么打**（WAL 一致性快照） | `scripts/backup.sh:367-368`「WAL 模式下 `cp` 会漏未合并日志，`.backup` 由 SQLite 内部保证快照一致」；加密默认开、`--require-encrypt` fail-closed（`:319-347`）；保留 30 天（`:63`） |
| 3.6 | **审计锚点必须随备份走** | `scripts/backup.sh:16-17`「缺了它恢复出来的库无法再自检『删尾 / 删前缀 / 整表清空』——锚点是审计链唯一的外部参照」；`:243-250` 锚点须与库**同批次**落位 |
| 3.7 | **升级只在 `migrate=True` 的路径发生** | `db.py:486-492`（`if migrate:` 门内才跑迁移）；`db.py:443-452`「只读校验类工具应传 `False`——迁移会重写审计链（v3 rechain）等，使『被校验对象在校验过程中被改动』」。调用点：`scripts/audit_verify.py`、`scripts/db_export.py`、`scripts/clock_guard_reset.py`、`scripts/rekey_accounts.py`、`yiban/cli.py` |
| 3.8 | **状态文件是跨进程契约** | `45-backend-design-spec.md:163-165`（§4.5）六类状态文件 JSON 结构不得改 |
| 3.9 | **JSON→SQLite 导入与 `.bak` 逃生门** | `migrations.py:897-909`（库**仍为空**才导入，幂等）、`:1012-1032`（改名 `.bak-<date>`、`0600`、同日递增序号）；生产库非空 ⇒ 本批**不触发** |
| 3.10 | **历史部署做法** | `docs/refactor/68-v044-deploy-record.md:26`（升级前建备份/回滚点目录 + `ROLLBACK.txt`）、`:27-28`（`git fetch` + 纯快进 + 重启服务）、`:33`（核验 `user_version` 与应用日志 `schema 迁移完成: v17_sign_claims`）、`:58-63`（回滚 = `git reset --hard` + 重启；**「v17 只新增表，回滚代码不需要回滚数据库（多一张空表无害）」**） |

## 4. 升级执行序（runbook）

> 本批**不动生产**；本节是升级批的执行依据。

1. **前置（窗口外 + 备份门）**
   - 确认当前不在签到窗口（`06:31`~`07:50` 之外），或确认无 in-flight 领取行：`SELECT COUNT(*) FROM sign_claims WHERE day=<today> AND state='claimed';` 期望 0。
   - **当日备份包必须存在**（I-6）：`ls -la /var/backups/yiban-$(date +%F).tar.gz.gpg`；不存在则先手工跑一轮 `yiban-backup-wrapper.sh --require-encrypt` 并确认出现 `.sha256` 侧车。
   - 另建回滚点目录（照 v0.4.4 模板）：库热备 + `.env.bak` + `HEAD.before` + `ROLLBACK.txt`。
2. **记录升级前基线**（§5 的前半张表）：`PRAGMA user_version`、逐表行数、`PRAGMA integrity_check`、`python3 scripts/audit_verify.py`。
3. **部署代码**：`git fetch` → `git merge --ff-only` → `systemctl restart yiban-web.service`。迁移在 `init_db` 里自动执行（不需手工命令）。
4. **核验**：`PRAGMA user_version` 应为 20；应用日志应出现 `schema 迁移完成: v18_sign_tasks` / `v19_fencing_epoch` / `v20_...`；若有 `blocked` 字样必须停下排查（§3.3。
5. **对账**：跑 §5 的全部判据 + `scripts/ledger_check.py`（T7 产出）。
6. **观测**：至少观察一个完整签到轮（06:31）与一次补签轮，确认退出码 0、失败数 0、`sign_claims`/`sign_tasks` 行数符合预期。

## 5. 数据保全与对账（验收门）

### 5.1 保全判据（可逐条打勾）

| # | 判据 | 命令（只读） | 期望 |
|---|------|-------------|------|
| A1 | `user_version` 单调上升 | `PRAGMA user_version` | 20 |
| A2 | **既有 10 表行数逐表不变** | 逐表 `SELECT COUNT(*)`（§1.1 基线：`accounts=90`*、`users=111`、`sign_claims=309`、`time_prefs=31`、`app_meta=8`，其余按升级前实测） | 与升级前**逐表相等**（`accounts` 会因自然注册增长，需按升级时点实测值比对） |
| A3 | `sign_claims` 行未被改动 | `SELECT state, COUNT(*) FROM sign_claims GROUP BY state` | 升级前分布一致（基线：`done=309`） |
| A4 | 平移行数 == 平移时点 `sign_claims` 行数 | `SELECT COUNT(*) FROM sign_tasks WHERE vshard=-1 AND owner!='backfill'` | 309 |
| A5 | 平移行状态分布与源一致 | `SELECT state, COUNT(*) FROM sign_tasks GROUP BY state` | 含 `done=309`（+ v20 补的行） |
| A6 | 无越界状态值 | `SELECT DISTINCT state FROM sign_tasks` | ⊆ {`pending`,`claimed`,`done`,`failed`,`stolen`,`skipped`} |
| A7 | 平移行惰性 | `SELECT COUNT(*) FROM sign_tasks WHERE vshard=-1 AND state='pending' AND day=<today>` | 对 v3 领取无影响（vshard=-1 永不进入分片集合；由 §8 第 6 条的单测钉住） |
| A8 | 库自洽 | `PRAGMA integrity_check` | `ok` |
| A9 | 审计链与锚点自洽 | `python3 scripts/audit_verify.py`（`migrate=False` 路径） | 链自洽 + 锚点一致 + 无写入欠账 |
| A10 | JSON 终态已补回 | `python3 scripts/ledger_check.py --all-days 14` | exit 0（无差异） |

### 5.2 升级前后对比表（升级时回填）

| 项 | 升级前（实测 2026-09-23） | 升级后（待回填） |
|----|--------------------------|------------------|
| `user_version` | 17 | 待回填 |
| `sign_claims` 行数 / 分布 | 309 / `done=309` | 待回填 |
| `sign_tasks` 行数 / 分布 | 表不存在 | 待回填 |
| 其余 9 表行数 | §1.1 | 待回填 |
| 签到轮结果（升级后首轮） | — | 待回填（期望 ✅ 全成、❌ 0、退出码 0） |

## 6. 回滚设计

**主路径：换回旧代码，库不动。**

- 依据：本批只增不改（§3.1）；`_run_migrations` 的 `version >= target_version: continue`（`migrations.py:846-848`）⇒ 旧代码打开 `user_version=20` 的库时**整链跳过、不报错、不改库、不降版本**。
- 先例：`68-v044-deploy-record.md:58-63`「v17 只新增表，回滚代码不需要回滚数据库」。
- 步骤：`git reset --hard <旧 HEAD>` → `systemctl restart yiban-web.service` → 核验 `user_version` 仍为 20 且 v2 链路正常。
- 回滚后残留：2 张新表与 `epoch` 列留存（旧代码不读，无害）；`sign_tasks` 里的行**不会**被旧代码使用。

**唯一需要回库的情形**：本次升级含**破坏性** schema 改动（改名/DROP/删列）。**本批没有**——故回库不是本批的回滚手段。若将来（v3 灰度批的退役阶段）引入破坏性改动，必须在该批单独设计回库路径与演练。

**演练要求**：升级批必须先在测试机用**生产库副本**跑一次完整「升级 → 对账 → 代码回退 → 再核验」，留下记录；不允许直接在生产首跑。

## 7. 首次发布前必须收口的小项

| # | 项 | 现状 | 处置 |
|---|----|------|------|
| 7.1 | `_ALLOWED_TABLES` 未收录 `egress_state`（`app_meta` 亦未收；`executor_heartbeats` **已按 R-T5 裁决从 v18 删除该表**（文件心跳已覆盖其职责）） | `migrations.py:157-159` 原为 12 项 | 补 `egress_state`、`app_meta` **两项**。作用域是 `_table_columns`/`_ensure_column`/`db._table_min_max` 三个助手的白名单（白名单外 `raise ValueError`）；已随 T7 落地 |
| 7.2 | 「v18 已上生产语义冻结」表述与事实不符 | `ledger-dual-version-design-20260923.md` §3.0 称 v18 冻结；实测生产 `user_version=17`，v18/v19 **从未发布** | 订正为「v18/v19 均未发布，首次发布前仍可修改；**发布即冻结**（I-8）」 |
| 7.3 | `ledger` 文档 I-5 的证据写成了「已实证 `user_version=18/19` 旧迁移链整体跳过」 | 那是本地库实测，不是生产 | 改为「机制依据：`migrations.py:846-848`；实证由 T7 的『旧代码冒烟』测试承担」 |
| 7.4 | v20 的编号唯一性 | `ledger` §3.6 曾把 v21 同时用于 backfill 与退役 | 已订正：v20=backfill、v21=改名、v22=DROP（后两者属灰度批） |

## 8. 逐行审查对照清单

审查者按此逐条核对；每条都要能指出「主张 → 证据（`文件:符号 @SHA` 或命令输出）」是否成立。

| # | 主张 | 证据 |
|---|------|------|
| 1 | 本分支对迁移文件的改动**只有新增**，未改任何既有迁移 | `git diff b6457e6..HEAD -- yiban/store/migrations.py`（+107/−2，新增 v18/v19 与登记项） |
| 2 | v19 的加列是**带默认值的加列**，不改行 | `migrations.py:805-806`（`INTEGER NOT NULL DEFAULT 0`） |
| 3 | v18 的平移**不改** `sign_claims` | `migrations.py:777-781`（`INSERT OR IGNORE … SELECT`，对源表只读） |
| 4 | 平移行**惰性**（不会被领取/窃取） | `hrw.shards_of` 产出 `0..V-1`；平移行 `vshard=-1` ⇒ 交集为空；配 `tests/test_migrations_v20.py` 的惰性用例（直证 `-1 ∉ hrw.shards_of(...)`）与 §8 第 6 条的旧代码冒烟用例 |
| 5 | v20 只读 `sign-state-*.json`，**不读** `sign-daily-*.json` | T7 实现 + `runner.py:538-560`（`sign-daily` 是符号表） |
| 6 | **旧代码冒烟**：`user_version` 高于旧代码迁移顶时，`_run_migrations` 不执行任何迁移且不抛 | T7 新增测试：把库置 `user_version=20` 后调 `_run_migrations(conn)`，断言无写入、无异常、版本不降 |
| 7 | 升级后 v2 行为不变 | `YIBAN_SCHEDULER_V3` 缺省 `0`；`migrations.py` 不改 v2 读路径（`claims`/`state_io` 未改语义） |
| 8 | 备份门存在 | §4 步骤 1（I-6）；`scripts/backup.sh` 的加密 fail-closed 与锚点同批次（§3.5/3.6） |
| 9 | 审计链不受升级影响 | 三步迁移不触碰 `audit_logs` 与 `app_meta` 的锚点键；A9 用 `migrate=False` 路径校验 |

---

## 明确缺陷

| 编号 | 缺陷 | 影响 | 处置 |
|------|------|------|------|
| M1 | v20（backfill）与 `ledger_check` 尚未实现 | §5 的 A10 判据目前无法执行；升级能跑完但台账对"已跳过"账号仍空白 | T7 落地 |
| M2 | 升级窗口约束（避免 in-flight `claimed` 行被平移成过去租约）只写在 runbook，**未在代码里强制** | 若在窗口内升级，会产生 `state='claimed'` 且 `lease_until` 过去的平移行；因 `vshard=-1` 惰性，**不会**导致重复登录，但台账会显示一笔假"在飞" | 靠 §4 步骤 1 的运维约束；若要求代码强制，需在 v18 增一条 `WHERE` 或加"窗口期禁止迁移"守卫（本批不做） |
| M3 | 未做升级演练 | 生产首跑风险 | 升级批必须在测试机用生产库副本演练（§6 演练要求） |
| M4 | `sign_claims` 的长期保留/清理策略未定（生产已 309 行，按日增长） | 体量增长后 v18 平移会变慢、备份变大 | 与本设计无关但需登记；归 v3 灰度批（届时 `sign_claims` 进入退役流程） |
| M5 | 升级前后的**实测值**未回填（§5.2） | 对账表暂时不完整 | 升级批执行时回填 |
| M6 | **A 案 §8 的迁移编号与本分支重号**（A 案 v19=events 迁库、v20=`vshard_hint`；本分支 v19=`fencing_epoch`、v20=backfill） | 后来者若照 A 案原文实施会改写已登记迁移项 | 已在 §2.4 显式对账并给出「顺延到 ≥v21」规则；**A 案原文未改**（属用户侧文档，如需加注请裁） |
| M7 | **补账/平移的 `vshard=-1` 行在 v3 侧永远不了结**：`failed ∈ TASKS_OPEN_STATES`，而 `vshard=-1` 永不被 `claim_batch` 领取、也无人能 requeue（无 owner/epoch 可对）；`queue_store.day_counts` 又不做 vshard 过滤 ⇒ 若 v3 的"当日了结"闸门读它，该日**永远不了结** | **当前无实害**（现行闸门读 `claims.stats`），但 T9/T10 接线后即生效 | 见待办 U7：闸门读台账时必须排除 `vshard=-1` 的历史行（或只读 `state ∈ {done,skipped} AND vshard >= 0`） |

## 待办

| 编号 | 事项 | 处置时点 | 状态 |
|------|------|---------|------|
| U1 | 订正 `ledger-dual-version-design-20260923.md` 的 §7.2 / §7.3 两处（v18 冻结表述、I-5 证据） | 即刻 | 已完成 |
| U2 | T7 落地 v20 backfill + `scripts/ledger_check.py` + **旧代码冒烟测试**（§8 第 6 条） | T7 | 已完成（`5c72ded`/`77410da`；独立复核见 `review-t7-independent-20260923.md`） |
| U3 | `_ALLOWED_TABLES` 补 `egress_state`/`app_meta` 两项（§7.1，不含 `executor_heartbeats`） | T7 | 已完成 |
| U4 | 升级批执行本文 §4 runbook，并回填 §5.2 对比表 | 升级批 | 未开始 |
| U5 | A 案 §8 未实现项（`sign_events` 迁 `events.db`、`accounts.vshard_hint`）**顺延到 ≥ v21**；`vshard_hint`（"固定账号虚分片以稳定跨天作息"）尚未纳入合并计划，需与 A 案 §12 自适应批一起排 | 拆库/自适应批 | 未开始 |
| U6 | 运维硬规则落地：v2→v3 切换**只在当日首轮之前**（§2.4）；写进 `docs/dev/scheduler-v3.md` 与升级/灰度 runbook | T9/T10 | 未开始 |
| U7 | **M7 的落地**：v3 的"当日了结/待办"闸门读台账时必须排除 `vshard=-1`（补账与平移的历史行），否则该日永远不了结；同时给 `ledger_check` 或闸门补一条"历史行不参与判定"的测试 | T9/T10 | 未开始 |
