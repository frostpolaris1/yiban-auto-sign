# 独立复核：T7（v20 backfill + 瘦对账）（2026-09-23）

> **是什么**：对 T7 两个提交（`5c72ded` v20 迁移、`77410da` 测试隔离与 v19 断言对齐）的对抗性复核，以及**发现项的处置分派**。T7 直接决定"生产库 17→20 升级时数据对不对"，故复核以数据正确性优先。
> **状态**：复核已完成；处置见 §3（修复轮待 T5/T6 修复轮落地后派发，避免同一 worktree 的 git 索引竞态）
> **范围**：只覆盖上述两个提交；不含 T8 及之后。
> **读者**：修复轮实施者必读；执行生产升级的运维选读；M2 审查者必读（这些是已知面）。
> **基线**：`feature/backend-scheduler-v3`，复核时 HEAD `77410da`

**关键词**：独立复核、v20 backfill、ledger_check、迁移幂等、假绿、前向风险、v1~v19 零改动

## 速览

- **结论**：通过但需修 4 处（均中危）；**无数据正确性阻断项**。
- **四条"未证伪"的核心主张**：v1~v19 **真零改动**（diff 仅 3 行删除，皆为 import 与白名单字面量，登记项为表尾追加）；backfill 映射与设计**逐键一致**（10 键恰为 `ALL_STATUSES(12) − {retrying, pending}`）；重跑收敛成立（含"部分批次已提交 + 某日抛异常"的组合）；`INSERT OR IGNORE` 不覆盖既有行。
- **四条要修**：① `test_migrations_v19` 的一处断言在追加 v20 后**静默改了测的对象**；② 冻结映射表**无守卫**绑定 `status.ALL_STATUSES`（新增终态码会"既漏账又报对账平"）；③ `ledger_check` 的非 sqlite 异常与"零覆盖"都让 A10 可能**假绿**；④ 补账的 `failed` 行在 v3 闸门下**永远不了结**（前向风险，非当前实害）。
- 另一条独立发现值得记：复核者核实**生产 v0.4.7 确实在写 `sign-state-<day>.json`**，故 A10 不是天然空转。

## 目录

1. 复核方式
2. 发现清单
3. 处置分派
4. 确认成立的部分（复核背书）

---

## 1. 复核方式

- 只取**已提交对象**（`git show <sha>` / `<sha>:<path>`）——复核期间另一代理正在同 worktree 改代码，此隔离必需；未跑 pytest。
- 复核者被要求**主动证伪**关键主张，并区分"测试真断言行为"与"同义反复"；额外自查了"v20 的输入在生产真的存在"这一外部前提。

## 2. 发现清单

**中**

| # | 位置 | 问题 | 依据 |
|---|------|------|------|
| 1 | `tests/test_migrations_v19.py::SchemaTest.test_v19_is_optional` | 断言写的是 `db._MIGRATIONS[-1][3] is False`（表尾项的 core 标志）；追加 v20 后 `[-1]` 指向 **v20**，用例名说"v19 是可选迁移"却实际测 v20；把 v19 改成 core 也照样绿 | `5c72ded` 在表尾追加 `(20,…)`；`77410da` 修了同文件另外两处"顶=19"硬编码，**漏了这第三处**（同类只修了 2/3） |
| 2 | `yiban/store/migrations.py::_JSON_TERMINAL_TO_TASK_STATE` | 冻结映射表**无任何守卫**绑定 `yiban.status.ALL_STATUSES`：将来新增一个终态码时，v20 静默不补、而 `ledger_check` 用同一张表 ⇒ **既漏账又报"对账平"**，无任何信号 | 表键 = 12 常量 − {retrying, pending}（今天恰好完整）；`tests/test_status_sets.py` 只钉 `status` 自身全集 |
| 3 | `scripts/ledger_check.py::main` / `_check_day` | ① 只捕 `sqlite3.Error`，其它异常（`.env` 解析、`clock` 抛错）让 Python 以 **exit 1** 退出，与"有差异"同码；② "状态目录在、但一天的文件都没有"仍 **exit 0** ⇒ A10 依赖的绿灯可以是**空覆盖的假绿** | `except sqlite3.Error` 只包住 init_db + 循环；缺失日只 append 一条"跳过"不计 problem |
| 4 | `migrations.migrate_v20` × `queue_store.day_counts` | 补账落 `failed` 的行在 v3 侧**永久 `open`**：`failed ∈ TASKS_OPEN_STATES`，而 `vshard=-1` 永不被 `claim_batch` 领取、也无人能 requeue（无 owner/epoch 可对）；`day_counts` 又不做 vshard 过滤 ⇒ 若 v3 的"当日了结"闸门读它，该日**永远不了结**（**当前闸门读 `claims.stats`，故暂无实害**，属接线前必须裁决的前向风险） |

**低**

| # | 位置 | 问题 |
|---|------|------|
| 5 | `tests/test_migrations_v20.py::test_backfilled_rows_are_lazy` | 只断言 `vshard == -1` 字面量，**没有**断言 `-1 ∉ hrw.shards_of(...)`（"惰性"靠组合推理而非直证）；设计 §8 第 4 条指向的"第 6 条单测"是**悬空引用** |
| 6 | `migrations.py` 模块头 | 仍写"迁移项 `migrate_v1..v19`"，本轮已加 v20（同文件内的陈旧自述；实施者为保持 diff 纯增量而故意留） |
| 7 | `upgrade-data-migration-design-20260923.md` §7.1 / 待办 U3 | 仍要求白名单补 **3** 项（含 `executor_heartbeats`），实现按订正后简报只补 2 项 ⇒ 文档未同步，M2 照设计读会判"漏项" |
| 8 | `tests/test_ledger_check.py` | 检查项 3 的**失败分支**（"第三个写者"）无用例；夹具行恒为 `vshard=-1 + owner='backfill'`，"平移行"分支从未走到 |
| 9 | `tests/test_migration_compat.py::_SEEDED_TABLES` | A2 要求"既有 **10** 表逐表行数不变"，用例只覆盖 4 张（缺 `audit_logs`/`app_meta`/`session_cache`/`sign_events`/`verify_jobs`/`user_delete_requests`）——等于简报⑤第 9 条，但**低于设计判据** |
| 10 | `scripts/ledger_check.py::_read_terminals` | 自己重写了一遍终态判定，未复用 `migrations._terminal_task_state` ⇒ 只共用常量表、规范化逻辑仍会漂移 |
| 11 | `migrations.py::_read_sign_state` | 异常文本直接入日志，未按 `state_io` 的 `_sanitize_text` 口径（实害为零，但与既有约定不一致） |

## 3. 处置分派

**立即修（T7 修复轮；排在本轮 T5/T6 修复之后）**

| 条目 | 修法要点 |
|------|---------|
| #1 | 不要把断言指向"表尾项"。照该文件既有的 `_init_at_v17` 模式**把登记表临时窄化到 ≤19** 再断言尾项 == 19 且 core=False；同时给 v20 补一条对应的登记断言（可选迁移） |
| #2 | 加一条**绑定守卫测试**：`set(_JSON_TERMINAL_TO_TASK_STATE) == set(yiban.status.ALL_STATUSES) - {STATUS_RETRYING, STATUS_PENDING}`；并在常量表注释里写明"新增状态码必须同步此表，否则 backfill 会静默漏项" |
| #3 | ① 把 `main` 的异常兜底扩到"任何异常 → exit 2"（只有明确探测到差异才 exit 1）；② 当**没有任何一天存在可用输入**时返回 exit 2（无法定论）而非 0，避免 A10 假绿；补两条测试 |
| #4 | 本批不改代码，**登记为前向风险**（见下）并写进 T9/T10 的范围：v3 的"当日了结"闸门读台账时必须**排除 `vshard=-1` 的历史行**（或改为只读 `state ∈ {done, skipped}` 且 `vshard >= 0` 的行） |
| #5 | 让该用例直证惰性：断言 `hrw.vshard_of` 的值域/`shards_of` 的集合**不含 -1**（即 `-1 ∉ 任何执行体的分片集`）；并修掉设计 §8 的悬空引用 |
| #6 | 模块头 `migrate_v1..v19` → `migrate_v1..v20` |
| #10 | 让 `ledger_check` 复用同一份终态判定（把 `_terminal_task_state` 提为可复用名，两处调用同一函数），消掉漂移面 |
| #11 | 异常文本走 `sanitize_text` 同口径 |
| #8、#9 | 补测试：#8 加"第三个写者"失败分支 + 一条 `vshard>=0` 平移行；#9 把 A2 的种子表补到**全部 10 张既有表** |

**登记（不修）**：无——#1~#11 全部纳入修复轮（#4 以"登记 + 写进 T9/T10"的形式落实）。

## 4. 确认成立的部分（复核背书）

- **v1~v19 真零改动**：`git show 5c72ded -- yiban/store/migrations.py | grep '^-'` 只有 3 行删除（diff 头、一行 import、`_ALLOWED_TABLES` 字面量行）；无迁移函数体/登记项被改写；版本单调。
- **映射与设计逐键一致**；`retrying`/`pending`/空串/缺键/非 dict 均不补；`utf-8-sig` 容 BOM；缺失/损坏 → warning 后跳日不抛；状态目录整缺失 → 0 行。
- **重跑收敛成立**（含"部分批次已提交 + 某日抛异常"）；`INSERT OR IGNORE` 不覆盖既有行（用 `owner='worker-0@host'` 的行反证过）。
- **只读 `sign-state`**：全 diff 无 `sign-daily` 的生产代码引用；`run_at` 格式可正确排序。
- `ledger_check` 三项检查与简报一致、退出码互斥、差异行脱敏、生产代码无 11 位手机号字面量。
- `test_migration_compat` 的旧代码冒烟确实钉住三件事：迁移项一个都没执行（炸弹函数）、零行写入（`total_changes` 前后相等）、版本不被降/抬。
- 新增注释无任务痕迹；19 例中未见同义反复。
- **`v20` 的输入在生产真实存在**：复核者核到 v0.4.7 经 `state_io._write_sign_state` 写 `sign-state-<day>.json`。
