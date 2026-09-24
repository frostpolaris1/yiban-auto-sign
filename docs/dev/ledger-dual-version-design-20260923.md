# 双版本数据适配与迁移设计（sign_ledger 统一台账层，2026-09-23）

> **是什么**：v2/v3 两个调度版本共存期的数据适配层设计 + 全周期数据迁移算法（backfill / 切换 / 回滚 / 退役）。
> **状态**：定稿（2026-09-23）
> **范围**：状态词汇表、统一台账门面、日写者模型、迁移四阶段算法、容量口径收口；不覆盖 §12 自适应体系、三库分离、asyncio 执行体内部结构（各归其批次）。
> **读者**：M1 验收与接线实施者必读；M2 审查者必读；M3 修复者选读。
> **基线**：feature/backend-scheduler-v3 @ `32aab79`（行号证据以此为准）

**关键词**：双版本、数据迁移、sign_ledger、口径统一、日写者、fold/fold_back、灰度、回滚、backfill、ledger、migration、v18、v19、v20、v21、v22、settled 语义

## 速览

- **三件套适配**：①状态词汇表（单一枚举 + 合并格 + 四个显式谓词）②`sign_ledger` 统一台账门面（消费者唯一读端口）③**日写者模型**（每个业务日恰好一个写版本）。
- **迁移粒度 = 自然日**：同日零双写，跨版本「恰好一次」由单写者平凡保证——不需要跨表分布式协调。
- **发现的迁移缺口**：v18 只平移了 sign_claims，**JSON 状态文件里的跳过类终态没进 sign_tasks**——backfill 补上，否则台账不完备。
- **回滚 = 反向折叠 fold_back**：与 v18 平移是互逆的同一算法（共享一张映射表），回滚不丢进度、不重签。
- 全部迁移操作幂等可重入；附对账工具规格（`ledger_check`）作为验收门。

生产升级与数据保全的完整设计（逐迁移 SQL、对账判据、回滚、runbook）见 `docs/dev/upgrade-data-migration-design-20260923.md`。

## 目录

1. 问题、双版本语义与不变量
2. 三件套设计（词汇表 / 台账门面 / 日写者模型）
3. 数据迁移算法（backfill / 切换 / 回滚 / 退役）
4. 并发与边界情形
5. 容量口径的双版本适配（收口）
6. 验收不变量与对账工具
7. 落地分阶
8. 自选时间片映射迁移（补充）
9. 现状核验记录（@32aab79）

---

## 1. 问题、双版本语义与不变量

**本节结论**：双版本 = v2（串行调度器：`sign_claims` 3 态 + JSON 状态文件）与 v3（队列调度器：`sign_tasks` 6 态），灰度期必须共存；当前的病是「三套状态源、三处硬编码字面量 + 一处枚举点、settled 同名不同义」，且消费者各查各的。

现状证据（@32aab79）：`claims.stats` 的 `settled=done`（定义 claims.py:337，赋值 `:349`）vs `queue_store.day_counts` 的 `settled=done+skipped`（定义 queue_store.py:239，赋值 `:267`）——同名不同义；queue_store.py:242 的 docstring 虽写「与 claims.stats 同口径」，但同一 docstring 在 `:251-253` 已自我澄清「state 词汇比 sign_claims 多两个（skipped 归入了结、stolen 归未了结）」——故病根是「同名不同义 + 跨模块无单一定义源」，不是「声称了同口径却不自知」。状态串是**三处硬编码字面量**（claims.py:59 的 3 个 `STATE_*`、queue_store.py:54 的 6 个、yiban/status.py:18 的 12 个）加**一处枚举点**（engine/round.py:75 的 `_CLAIM_DONE_STATUSES`，由 `yiban.status` 常量组合而成，非字面量）——共四处需要收口。消费者分读三源：`state_io.has_undone_accounts_today` 优先 claims 回退 JSON（定义 state_io.py:327，主体至 `:354`）、`_second_run_drop_done` 只读 JSON（state_io.py:148）、`latest_claims_day` 只读 claims（定义 claims.py:397，SQL `:407`）、web 我的账号视图只读 JSON（my.py:91，经 `web/services/logs.py` 的 `load_sign_state`，定义 logs.py:212）。

**双版本定义**：
- **v2 版**：`build_schedule` 5 分钟块 + `round.py` 堆队列 + `claims` 领取池 + JSON 逐步状态。
- **v3 版**：`planner` 双粒度计划 + `sign_tasks` 批量领取 + token_bucket + asyncio 执行体。

**不变量（本设计的验收口径）**：
- **I-1 恰好一次**：每 (phone, 业务日) 跨两版本至多一条成功终态（真实登录不重复）。
- **I-2 单义**：任意文档/代码里「settled」「了结」只有一个定义源，消费者不再各自解释。
- **I-3 幂等可重入**：backfill / 切换 / 回滚 / 任一迁移步，崩溃后重跑收敛到同一结果。
- **I-4 回滚不丢进度**：v3 切回 v2 后，v3 当日已做的成果在 v2 视图内可见，且不被重做。
- **I-5 旧版无感**：迁移后的库对 v0.4.7 旧代码零破坏。机制依据：`yiban/store/migrations.py:846-848` 的 `if version >= target_version: continue` ⇒ 旧代码打开 `user_version` 更高的库时**整链跳过、不报错、不改库、不降版本**；未知表对旧代码不可见，`sign_claims` 保留可读写。（**实证由 T7 的「旧代码冒烟」测试承担**：把库置 `user_version` 高于旧代码迁移顶后调 `_run_migrations`，断言零写入、零异常。生产实测 `user_version=17`，v18/v19 尚未发布。）
- **I-6 备份先行**：动迁移前置条件是当日备份包存在（复用备份哨兵判据，09-23 修复的 B1/B2 链路）。

## 2. 三件套设计

**本节结论**：词汇表治「同名不同义」，门面治「各查各的」，日写者治「跨版本重复签」——三者分别对应 I-2 / 消费者统一 / I-1。

### 2.1 状态词汇表 `yiban/store/ledger_states.py`（新增，唯一定义源）

**规范状态**（CanonicalState，六态与 sign_tasks 对齐）：`PENDING / CLAIMED / DONE / FAILED / STOLEN / SKIPPED`。

**合并格（merge lattice）**——跨源不一致时的合取规则，按「信息强度」定秩：

```
rank: PENDING(10) < CLAIMED(20) < STOLEN(25) < FAILED(30) < SKIPPED(40) < DONE(50)
join(a, b) = rank 高者胜；同秩不同名（仅 FAILED vs STOLEN 理论可能）取左侧源先者
```

设计理由：DONE 是不可逆事实（真实登录发生了），吸收一切；SKIPPED 是管理决策，强于失败猜测；FAILED 弱于 SKIPPED（失败可能是错判，跳过是明示）。**DONE 永不被覆盖**即 I-1 的代数形态。次序可争之处见尾部 D2。

**四个显式谓词**（治病根「一个词两种用」——不同机制要的集合本就不同，现在分开命名；每个都钉住它与现行旧口径的等价关系，这是本设计的验收依据）：

| 谓词 | 成员 | 等价旧口径 | 服务对象 |
|------|------|-----------|---------|
| `is_successful` | {DONE} | — | 日历展示、成功率 |
| `is_concluded` | 全部非 PENDING 状态 | `state_io._has_conclusion`（`status` 非空且非 `pending`，state_io.py:87-96） | 「已有结论」透传（E2 机制）、防重复留痕（`only_if_absent` CAS） |
| `needs_second_run` | {PENDING, CLAIMED, STOLEN, FAILED} | `yiban.status.UNDONE_STATUSES`（status.py:67-71） | 补签轮判据、兜底扫描 |
| `drop_done`（补签剔除集） | {DONE} | `round._CLAIM_DONE_STATUSES`（round.py:75）与 `state_io._second_run_drop_done`（state_io.py:162-165） | 补签轮定向重跑剔除 |

`is_concluded` 与 `needs_second_run` **不是互补关系**——`CLAIMED` 同时属于两者（这正是 JSON 里 `retrying` 的现行语义：既有结论、又未了结），这是刻意的，勿「修」掉。

**旧表述的缺陷（本设计据此改口径的理由）**：旧映射只显式点名 7 个常量、其余靠「其它→FAILED」兜底，会把 `skipped_window`/`skipped_norange`/`no_position` 映射到 SKIPPED（因而被排除出 `needs_second_run`）；但 `yiban/status.py:67-71` 的 `UNDONE_STATUSES` **把这三个状态计为「未了结」**（会触发补签轮）——按旧表述实施会造成**行为变化**，与 D1「按现行为实施」矛盾。故改为上表：每个谓词的成员集合与其等价旧口径逐一对齐。

FAILED 的双层语义据此显式化：**窗口内不再自动退避重试**（`is_concluded` 真），**但补签轮/手动可再试**（`needs_second_run` 真）——这同时满足 intended-design L3-3「failed 是终态（不再自动重试）」与现行 `has_undone` 把 failed 计未了结的实际行为，两文档的表面矛盾以此消解（确认见尾部 D1）。

**映射函数**（全函数、含缺省）：

```
from_claims_state（claims.py:59-66，3 态）：claimed→CLAIMED、done→DONE、failed→FAILED
from_tasks_state （queue_store.py:54-65，6 态）：恒等
from_status_json （yiban/status.py:18-37 的 12 个常量全集 → 6 态）：
```

| JSON status | canonical | 理由 |
|---|---|---|
| success / already / no_task | DONE | 今日不必再签，等价 `_CLAIM_DONE_STATUSES` |
| failed | FAILED | 最终失败：窗口内不自动重试（`is_concluded` 真）、补签可再试（`needs_second_run` 真） |
| skipped_window / skipped_norange / no_position | **FAILED** | 三者都在 `UNDONE_STATUSES` 内 → 必须落可重试侧；语义是「今天没签成」而非「管理决策」 |
| retrying | **CLAIMED** | 在 `UNDONE_STATUSES` 内（未了结）且 `_has_conclusion` 为真（有结论）→ 必须同时 ∈ `is_concluded` 与 `needs_second_run` |
| pending / 空串 / 缺 `status` 键 | PENDING | `_has_conclusion` 的唯一排除项 |
| paused / user_cancelled / global_paused | SKIPPED | 不在 `UNDONE_STATUSES` 内（管理/系统决策，不再重试），但 `_has_conclusion` 为真 |
| 未知串 | FAILED | 失效方向偏安全（宁多跑不漏签）；**与现行 `in UNDONE_STATUSES`（未知不算未了结）不同**，属显式记录的安全向偏离（见缺陷表 D6） |

claims.py、queue_store.py、status.py、round.py 四处硬编码/枚举点全部改为 import 本模块；`claims.SETTLED_STATES` 与 `queue_store.SETTLED_STATES` 的分歧由「统一改为 `is_*` 谓词」消掉，两侧 docstring 的「同口径」改为指向本模块。

### 2.2 统一台账门面 `yiban/store/sign_ledger.py`（新增，消费者唯一读端口）

```python
@dataclass(frozen=True)
class DayLedger:
    day: str
    writer: str                    # 'v2' | 'v3' | ''（未决主）
    by_phone: dict[str, CanonicalState]   # 已按合并格归一
    counts: dict[str, int]         # 六态计数 + settled/open/total（唯一定义）

def snapshot(day) -> DayLedger          # 每源一次批量查询（claims stats / tasks day_counts / JSON 一次读）
def is_settled(phone, day) -> bool      # 用 needs_second_run 的补集
def undone_phones(day, active_phones) -> set[str]
def latest_day() -> str | None          # 取代 claims.latest_claims_day（跨源取 MAX）
def materialize(day, out_path) -> None  # 从 DayLedger 生成 sign-state JSON（保读侧契约，O(N)）
```

**合并算法**（snapshot 内）分两条路径，**判定类不套合并格**（以构造保证零行为变化）：

- **判定类**（`is_concluded` / `needs_second_run` / `drop_done` / `undone_phones`）：**按日择主源**——当日 writer 已决则取 writer 源；未决则复现现行口径「claims 当日有行即以 claims 为准，否则 JSON」（`has_undone_accounts_today`，state_io.py:336-354）；**主源缺该 phone 时整源回退**，不做逐源混合。原「三源各自读出、逐 phone join」用于判定会与现行「按日整源择一」分叉，故判定路径明确不套合并格。
- **展示类**（`snapshot` 的 `by_phone`/`counts`、`materialize`）：套**合并格**做逐 phone `join`（DONE 永胜），信息更全且不影响判定。迁移期 sign_tasks 的行来自「v18 平移 + backfill + v3 新写」，claims 来自 v2 新写，JSON 来自 v2 逐步写。

两类查询代价都是每源一次批量（claims 聚合查询 / tasks 聚合查询 / 一次 JSON 读），不再逐账号。

**消费者切换清单**（逐个改 import，行为等价测试护航）：

| 消费者 | 现读 | 改读 |
|--------|------|------|
| `state_io.has_undone_accounts_today` / `need_second_run` | claims 优先、JSON 回退（定义 state_io.py:327，主体至 `:354`） | `sign_ledger.undone_phones` / 谓词 |
| `state_io._second_run_drop_done` | 只读 JSON（state_io.py:148） | `sign_ledger.snapshot` |
| `claims.latest_claims_day`（定义 claims.py:397，SQL `:407`） | 只读 claims | `sign_ledger.latest_day` |
| web 我的账号视图（my.py:91，属 `_my_account_view`@76，经 `logs.load_sign_state`@212；日历/日志接口分别是 `api_my_calendar`@590 / `api_my_logs`@630） | 只读 JSON | 优先 `snapshot`，缺失时 `materialize` 兜底（读契约不变） |
| web 执行体归属（executor_env.py:69/79 与 108/121） | 只读 claims | `sign_ledger`（owners 统一口径） |
| runner 容量预检/收尾统计 | load_accounts + claims/stats | `snapshot(day).counts` |

**适配层落点理由**（为什么不塞 db.py / state_io）：db.py 已是 698 行再导出门面，再加仓储变 god-module；state_io 属 engine 层，让它定义存储口径超出其职责——依据是本包的职责声明（`yiban/store/__init__.py:2` 自述为「数据访问层（SQLite 表级 CRUD）」）。独立模块最自然，且与 queue_store/claims 平级形成「三表一视图」。

### 2.3 日写者模型 `ledger_days`（跨版本恰好一次的平凡化）

```sql
CREATE TABLE ledger_days (
    day        TEXT PRIMARY KEY,        -- 业务日（北京日）
    writer     TEXT NOT NULL,           -- 'v2' | 'v3'
    decided_at TEXT NOT NULL,
    closed_at  TEXT NOT NULL DEFAULT '' -- 封账时刻（一次性）：日终对账通过后盖章
);
```

**规则**：
1. **每业务日恰好一个写者**：v2 或 v3，在该日首次领取前用 `INSERT OR IGNORE` 决主（PK 冲突即失败方）——天然原子 CAS，无锁。
2. **领取边是唯一强制点**：v2 的 `claims.try_claim` 与 v3 的 `queue_store.claim_batch` 入口先查当日 writer，不符者拒绝领取并告警（manual `--only` 与 fallback 走同一领取边，自动纳入）。
3. **同日单写者 ⇒ I-1 平凡成立**：同版本内靠 PK (phone,day) + fencing epoch（已实现），跨版本靠「只有一个版本在写」——不需要跨表协调。
4. **封账**：日终（收尾汇总时）盖 `closed_at` 并把 `counts` 快照写入 sign-daily 元数据，`need_second_run` 对已封账日直接答否——顺手消掉「哪一轮是最后一轮」的歧义（封账即最后一轮）。

**为什么不用「同日双写」**：双写需要跨表原子（两文件两事务，SQLite WAL 不保证跨库原子）或补偿逻辑，且 claims/tasks 的 settled 语义差异会让双写每步都要翻译；日写者把协调成本降到一次 `INSERT OR IGNORE`。代价是「切换要等日界」——灰度切换本来就是天级动作，这个代价为零。

## 3. 数据迁移算法

**本节结论**：四步走——**backfill（补 JSON 缺口）→ 切换（日粒度决主）→ 回滚（fold_back 反向折叠）→ 退役（改名保留再删）**；forward/back 共用一张映射表与同一 `fold` 算法骨架，全部幂等。

### 3.0 共用抽象：`fold(source_rows, target, direction)`

```
映射表 FOLD_MAP（与 2.1 的 from_* 映射同源）：
  direction='forward' ：claims/JSON → sign_tasks（v18 平移 + backfill 用）
  direction='backward'：sign_tasks → claims + JSON 物化（fold_back 用）
语义：INSERT OR IGNORE + PK (phone,day)，目标已有行不覆盖（join 格保证目标行只强不弱）。
幂等：重跑 OR IGNORE 全跳过；部分失败重跑收敛。单事务批量提交（50 行/批，防长事务）。
```

v18 里的平移 SQL（migrations.py:777-781）即 forward 的特例；实施时统一收进本抽象，**v18 的平移段落不重写**——理由不是已发布冻结，而是它已落地于本分支并被 `tests/test_migrations_v18.py` 断言、且语义正确。注意：生产实测 `user_version=17`，v18/v19 **从未发布**，故首次发布前仍可修改（发布即冻结，见 I-8）；`migrations.py` 的「已发布迁移不可再改」只对**已上生产**的迁移生效。迁移顶现为 **v19**（migrations.py:832，`v19_fencing_epoch`，本分支 `a2bc227` 引入，给 `sign_claims`/`sign_tasks` 各补 `epoch` 列），本设计新增迁移从 v20 起编号。目标表 `sign_tasks` 的 DDL 事实（v18，migrations.py:735-755）：`PRIMARY KEY (phone, day)`，列 `phone/day/vshard/owner/run_at/priority/state/attempts/lease_until/epoch/result/created_at`；`run_at` 是 `YYYY-MM-DD HH:MM:SS.mmm` 字符串（`planner.build_plan` 的产出行是普通 dict，非 dataclass）。

### 3.1 Phase 0 — 备份门

前置检查：当日 `/var/backups/yiban-<date>.tar.gz.gpg` 存在（B4 哨兵同判据），否则拒绝执行任何迁移步并告警。I-6。

### 3.2 Phase 1 — schema（v20 迁移，可选迁移口径与 v18 同）

**v20 = `ledger_days` 建表 + backfill 补 JSON 终态**，合并为同一步可选迁移（两者都幂等，backfill 见 3.3）。不 ALTER 既有表；幂等（IF NOT EXISTS + OR IGNORE）。**v21 = `sign_claims` 改名 `sign_claims_legacy`**、**v22 = DROP legacy + 删 v2 代码路径**（见 3.6）。

v18 另建 `egress_state` 一表（`executor_heartbeats` 已按 R-T5 裁决**从 v18 删除**：它无访问层，执行体存活由 `state_io.mark_worker_beat`/`worker_presence` 的文件心跳承担）；`egress_state` **有访问层**——`queue_store.load_egress_state`(:194) / `save_egress_state`(:215)，消费者是 `yiban/engine/token_bucket.py` 的 `EgressLimiter.persist/restore_from_store`（token_bucket.py:222/234）。现状：`EgressLimiter` 目前无生产调用点（仅模块内与测试）。

### 3.3 Phase 2 — backfill：补 v18 漏掉的 JSON 终态（本设计发现的缺口）

v18 只 `INSERT OR IGNORE ... SELECT FROM sign_claims`，而 claims 只记**真实领取过**的账号；JSON 里的**跳过类终态**（paused / user_cancelled / skipped_window）与手工结论不在 claims ⇒ 直接上 v3 会把「已跳过」当「未了结」重处理。

```
for day in json_days(retention=14, 默认与 claims 保留期同):
    rows = load sign-state-<day>.json                            # 只读这一个文件；utf-8-sig 读（BOM 教训 E4）
    fold(from_status_json(rows), sign_tasks, 'forward')          # state ∈ {DONE, SKIPPED, FAILED}
```

**输入只有 `sign-state-<day>.json`**：`sign-daily-<day>.json` 由 `yiban/engine/runner.py:538-560` 写成 `{phone: 符号}` 的**符号表**（只含 5 种状态、无 `time` 字段），既非状态码、也无时刻，`from_status_json` 对它无效，**不得**作为 fold 输入（见尾部缺陷表 D7）。

幂等由 OR IGNORE + PK 保证；vshard 置 -1（历史行不参与分片，同 v18）；owner 置 `'backfill'`。运行窗口默认 14 天回看，参数化；与 v20 同事务批提交，崩溃重跑安全（I-3）。

### 3.4 Phase 3 — 切换（日粒度）

```
T-1 日终：ledger_check 对 T-14..T-1 全部对账通过（§6），留档基线
T 日窗口开启（或首次领取前）：INSERT OR IGNORE ledger_days('T', 'v3', now)
T 日全天：v3 唯一写者；v2 进程若被误拉起，领取边拒绝（2.3 规则 2）并告警
T 日收尾：对账 → 盖 closed_at → materialize(T) 生成 JSON（读侧契约不破）
```

**影子期前置**（设计 §8 的 dry_run）：v3 影子模式只产计划不写领取行（计划行可落 sign_tasks 的 `state='pending'` 但 owner 前缀 `shadow:` 且领取边拒领），与 v2 真实落点对比分布 3 天后才允许决主 T 日为 v3。影子与真实用 owner 前缀区分，对账工具天然过滤。

### 3.5 Phase 4 — 回滚：`fold_back(day)`（与 forward 互逆）

```
① fold(sign_tasks 终态行, sign_claims, 'backward')   -- DONE→done(claims 词表)，SKIPPED/FAILED 同理映射
② materialize(day) 写 JSON                            -- v2 的逐步读口立即看到
③ UPDATE ledger_days SET writer='v2' WHERE day=? AND writer='v3'   -- 条件改，防并发争
④ v2 下一轮：E2「已有结论透传」机制自动跳过已折叠账号（round.py 的 _mark_window_skip 已具备此语义）
```

I-4 的保证链：fold_back 把 v3 的 DONE 落进 claims（claims 的 DONE 即「已成功」），v2 领取前查 claims 见 done ⇒ 不重签；v3 残骸进程再写 sign_tasks 会被 ③ 的 writer 判定 + fencing 双拦。**回滚丢失的只是「未做部分的计划」，已做部分零丢失**。

### 3.6 Phase 5 — 退役

观察期 ≥ 1 个 claims 保留周期（14 天，全 v3 日无回滚）→ **v21**：`ALTER TABLE sign_claims RENAME TO sign_claims_legacy`（只改名不 DROP，读旧报表可查）→ JSON 读端固化走 `materialize` → 30 天后 **v22**：DROP legacy + 删 v2 代码路径（届时把**待新建**的版本开关固定为 1；该开关默认 0、需显式开启）。退役后 `sign_tasks` 是唯一事实源，门面保留（消费者零再改）。

## 4. 并发与边界情形

**本节结论**：所有边界都收敛到「领取边判定」与「格合并」两个机制，无特例分支。

| 情形 | 处置 | 手段 |
|------|------|------|
| 跨零点迟到写（23:59 的任务 00:01 settle） | 落**原业务日**台账 | settle 显式携 day（现口径不变），日不随墙钟翻 |
| 决主竞态（两进程同刻起跑） | 一方败 | `INSERT OR IGNORE` 的 PK 原子性 |
| 迁移中崩溃 | 重跑收敛 | fold 幂等 + 迁移框架 BEGIN IMMEDIATE（migrations.py:858 现成；`_begin_immediate` 本体在 db.py:535） |
| 回滚后 v3 残骸进程 | 写被拒 + 告警 | 领取边 writer 判定 + fencing epoch 双拦 |
| manual --only 与自动轮并发 | 同版本内互斥照旧 | 同一领取边、同一 PK |
| 两源同秩冲突（理论极少） | 确定性择一 | 合并格定序（2.1） |
| JSON 缺文件日 | 该日 backfill 跳过，台账以两表为准 | fold 输入可空 |
| 时钟回拨 | 已有 clock_guard 告警；day 由调用方显式传入不受墙钟影响 | 现机制复用 |

## 5. 容量口径的双版本适配（收口）

**本节结论**：容量计算不是「v2/v3 公式打架」，而是 **v3 公式未被使用**——生产四处调用点全走 v2 `capacity_accounts`，`capacity_accounts_v3` 零生产调用点；收口办法是**一个选择函数、按当日 writer 取公式**，一次性消掉「保存被拒、计划排得下」的分裂。

```
capacity_of(day_or_mode, W, k, ...) -> int
  writer='v2' → capacity_accounts(...)          # 旧串行公式，v2 语义不变
  writer='v3' → capacity_accounts_v3(...)       # §6.1 修正版；k 自动 = clamp(ceil(N×(1+r)/(W×bucket×0.8)), 1, 出口数)
```

现行签名：`capacity_accounts(window_sec, gap=0, avg=None)`（schedule.py:97，公式 `slack//(avg+gap)+1`，其中 `slack = window_sec - avg`）；`capacity_accounts_v3(window_sec, k=1, avg=None, bucket_rate=1.0, util=0.8)`（schedule.py:118）。

web 保存闸门（capacity.py:91）、runner 超载告警（runner.py:375）、实测换算（settings_api.py:1090）、CLI 换算（cli.py:354）**四处**统一调 `capacity_of`——v2 日按 v2 公式（行为不变），v3 日按 v3 公式（误拒区间 361~3744×K 随切换自动消失）。K 自动公式在 v3 侧一并落地。`capacity_accounts_v3` 目前零生产调用点（仅 `tests/test_planner.py` 引用），故现状是「v3 公式未被使用」而非「两套公式打架」。vshard 集中校验与 density 单桶口径归 §12 批（已在 must-fix 登记 MF-2/MF-3），本设计不展开。

## 6. 验收不变量与对账工具

**本节结论**：`ledger_check` 是迁移的唯一验收门——三方对账（台账 vs claims vs JSON），非空 diff 即红。

```
python scripts/ledger_check.py --day D [--all-days N]
  ① snapshot(D) 与 claims(D) 的 join 差异列表（应只含 'backfill'/'shadow' 来源行）
  ② snapshot(D) 与 JSON(D) 经 from_status_json 的差异列表（DONE 不得出现在差异里）
  ③ settled 计数 = is_* 谓词直算数（词汇表自洽）
  exit 0=对账平；1=有差异（打印逐行）；2=无法定论（源缺失）
```

不变量对账：I-1 → 抽查任意日 DONE 唯一性；I-2 → 全仓 grep 硬编码状态串应为 0 命中（对照证明后断言）；I-3 → 迁移步重跑前后 check 输出一致；I-4 → fold_back 演练后 v2 视图 ⊇ 折叠前 v3 进度；I-5 → 旧代码开新库冒烟（现成测试补一条）；I-6 → 迁移脚本入口内置备份门。

## 7. 落地分阶

**本节结论**：A/B 两阶可与 M1 任务 6/7 并轨（接线期正好要动消费者），C/D 是灰度动作。

| 阶 | 内容 | 依赖 | 验收 |
|----|------|------|------|
| A | ledger_states + sign_ledger + 六处消费者切换 + claims/queue_store 枚举收口 | 无（v3 不上线即生效，settled 分叉当天治好） | 契约测试：三源等价、I-2 grep 0 命中 |
| B | v20（ledger_days + backfill）+ ledger_check + fold 抽象收口 | A | check 全平 + 重跑幂等 |
| C | 影子 3 天 → 决主切换演练（测试机 mock 360 账号链路）→ 生产 T 日 | B | I-1/I-4 演练记录 |
| D | 观察期 14 天 → v21 改名保留 → 30 天后 v22 DROP | C | 旧报表可查、回滚窗口关闭 |

---

## 8. 自选时间片映射迁移（补充）

**本节结论**：time_prefs 数据零迁移（偏好非台账），但**映射几何的归属与准绳必须迁移**——`_slot_to_bi` 挂在 v2 的 schedule.py 下却被 v3 planner 反向依赖、web 另有一份同口径独立重实现，v2 退役会拔断 planner；顺带统一 E9 的 bounds 准绳分叉。

### 8.1 数据面：零迁移

`time_prefs` 表（`phone`(PK) / `slot_min` / `updated_at`，由基线 bootstrap `_create_tables` 建立、**不属于任何编号迁移**，migrations.py:129-135）**不参与** fold/backfill——它是用户偏好不是每日台账。slot_min 语义（窗口相对偏移、5 对齐）原样钉住；管理员改窗口配置会整体平移落点属既有行为，迁移不加重也不修（超出本设计范围）。

### 8.2 映射面：几何函数中立化 + 准绳统一（真正的迁移对象）

现状三件散落：`schedule._slot_to_bi`（schedule.py:380-398，签名 `_slot_to_bi(cfg)`，返回 `{窗口相对偏移分钟: 块索引}`，**v2 模块**）、web 片枚举 `_pref_slots`（my.py:175-208，**自己重算了一遍同口径几何**：`off`/`lo`/`hi`/`disabled`，判据 `hi > lo`）、`_slot_to_label`（家在 accounts_data.py:316，web/app.py:1091 是转发壳，my.py:279/378/405 与 accounts_api.py:90 只是调用点）。v3 的 planner 反向依赖 `schedule._slot_to_bi`（planner.py:34 import、:253 调用）——Phase 5 删 v2 路径会让 **planner 断**，web 不受影响（web 零引用 `_slot_to_bi`）。

处置两条：
1. **归属中立化**：几何三件迁入 `yiban/window.py`（片几何本属窗口域），schedule / planner / my.py 改 import；迁移后 `grep -rn "_slot_to_bi" yiban/engine/schedule.py` 除注释外应 0 命中。**可行性已核验**：`window.py` 是叶子模块（window.py:21-22 只 import `datetime`/`os`，不 import 任何 `yiban.*`），依赖方向是 engine→window 单向（schedule.py:21、planner.py:33 已 import window）——几何迁入**无循环 import 风险**。迁入时顺带订正 `time_prefs.py:123` 与 `accounts_data.py:317` 的「06:30 → 390」注释（绝对分钟制写法，与代码实际的「窗口相对偏移」语义自相矛盾，登记 T6）。
2. **准绳统一（修 E9）**：`_slot_to_bi`（schedule.py:380-398）现**不套** `window.bounds()` 的「裁剪吃空回退默认窗口」，与 `planner.py:90` 的 `_span()`（走 `window.bounds`）形成同模块内两套 window 访问方式。裁剪切为 `fell_back` 时 `_slot_to_bi` 对全部块恒有 `hi<=lo`、**返回空 dict**，于是 v2 `build_schedule`（schedule.py:478）与 v3 `_place_prefs`（planner.py:268）各自 `logger.warning(… 不在今日可选范围，回退自动分配)` 并回退自动分配——**不丢号，但所选片被放弃，且只有日志级 warning、无管理员告警/UI 提示**。迁入时统一为套 bounds 的回退语义，v2/v3/web 同一准绳。

### 8.3 双粒度映射契约（跨版本不变量，新增 I-7/I-8）

- **I-7 自选片落点跨版本一致**：同一 prefs + 窗口，v2 `build_schedule` 与 v3 `planner.build_plan` 的落点 ∈ 所选 5 分钟片内（片满溢出除外，溢出按就近且距离最短，§3.3 语义）。
- **I-8 片枚举同源**：web `_pref_slots` 的可用片集合 = planner `_pref_slices` 的候选集 = `_slot_to_bi` 成员性判定（唯一准绳）。
- **满员阈值差异是设计改进非迁移风险**：v2 块容量 15/5min 即溢出顺延，v3 片内容量 300（5×60 槽）内层先吸收、满则 ±5/±10 外溢——自选命中率只升不降；web 拥挤度显示保留 block_cap=15 的用户契约口径（与 planner 实容量不同源，挂 D4 同类）。容量数字出处：v2 块容量 15 = `schedule.py:31 _DEFAULT_BLOCK_CAP`（配置键 `YIBAN_BLOCK_CAP`，schedule.py:199；web 侧 my.py:257/367/413 同默认）；v3 片内容量 300 = `planner.py:41 SLICE_SEC=60` / `:46 SLOTS_PER_SLICE=60` / `:335`（5 个 1 分钟分片 × 60 槽）。

---

## 9. 现状核验记录（@32aab79）

**本节结论**：把全文事实断言逐条对齐基线 `32aab79` 的实测结果，原描述与实测不一处全部订正；核心主张全部成立。

| 原描述 | 实测 | 处置 |
|--------|------|------|
| A1 状态串四处各自硬编码 | 三处硬编码字面量（claims.py:59 的 3 个 / queue_store.py:54 的 6 个 / status.py:18 的 12 个）+ 一处枚举点（round.py:75 由 `yiban.status` 常量组合） | §1 改「三处字面量 + 一处枚举点」 |
| A2 docstring 声称同口径却不自知 | 同 docstring `:251-253` 已自我澄清；病根是「同名不同义 + 无单一定义源」 | §1 定性订正 |
| A3 消费者行号 | `claims.stats`@337（`:349`）、`day_counts`@239（`:267`）、`has_undone`@327-354、`_second_run_drop_done`@148、`latest_claims_day`@397（`:407`）、`load_sign_state`@212（原引 `:223` 为函数体内路径行） | §1/§2.2 行号订正 |
| A4 my.py:91 属日历/日志接口 | 属 `_my_account_view`@76；日历 `api_my_calendar`@590、日志 `api_my_logs`@630 | §1/§2.2 改为「web 我的账号视图」 |
| A5 executor_env.py:107,121 | `_last_executors`@69（读 `claim_owners_for_day`@79）、`_executor_activity`@108（读 `claim_activity`@121） | §2.2 订正 |
| B1 `from_status_json` 兜底把 skipped_* 落 SKIPPED | 与 `UNDONE_STATUSES`（status.py:67-71 含三态）矛盾，会造成行为变化 | §2.1 按四谓词等价关系重写 |
| B2 三个显式谓词 | 应为四个（is_successful/is_concluded/needs_second_run/drop_done），且 is_concluded 与 needs_second_run 非互补（CLAIMED 同属两者） | §2.1 换表 |
| B3 映射只点名 7 常量 | status.py:18-37 是 12 常量全集；补全 12 + 空/未知映射 | §2.1 补全表 |
| B4 `from_claims_state`/`from_tasks_state` 未写全 | 补 claims 3 态、tasks 6 态恒等 | §2.1 补 |
| B5 依据「store 收口纪律」（store/__init__.py:2） | `:2` 只是「数据访问层（SQLite 表级 CRUD）」职责声明，无「纪律」字样 | §2.1 改「依据是本包职责声明」 |
| C1 合并算法三源逐 phone join | 判定类须「按日择主源」（`has_undone` 语义 state_io.py:336-354），逐 phone join 会分叉 | §2.2 拆判定类/展示类 |
| C2 db.py 529 行 | 698 行（基线亦 698） | §2.2 订正 |
| D1 迁移顶 v18、未提 v19 | `_MIGRATIONS` 顶为 v19（migrations.py:832，`v19_fencing_epoch`，`a2bc227` 引入补 epoch 列） | §3.0 补述 |
| D2 v21 用了两次（backfill / 退役改名） | 版本号复用 | 统一 v20=建表+backfill、v21=改名、v22=DROP（§3.2/3.3/3.6） |
| D3 平移 SQL migrations.py:776、BEGIN IMMEDIATE :857 | 777-781、`:858`；`_begin_immediate` 本体 db.py:535 | §3.0/§4 订正 |
| D4 I-5 `user_version=18` | 实库 `PRAGMA user_version = 19` | §1 订正 |
| D5 `executor_heartbeats`/`egress_state` 并称死表 | 仅 heartbeats 是死 DDL（migrations.py:760 + 测试断言）；egress_state 有访问层（queue_store:194/215 → `EgressLimiter.persist/restore`@222/234） | §3.2 分述；补「EgressLimiter 无生产调用点」；**该表已于 2026-09-23 按 R-T5 从 v18 删除** |
| D6 time_prefs 属编号迁移、列仅两列 | 由 bootstrap `_create_tables`（migrations.py:129-135）建，列 `phone`(PK)/`slot_min`/`updated_at` | §8.1 订正 |
| D7 sign_tasks DDL 事实缺 | v18（migrations.py:735-755）：`PK(phone,day)`，列含 epoch/result 等，`run_at` 为 `YYYY-MM-DD HH:MM:SS.mmm` 字符串 | §3.0 补 |
| E1 v2/v3 公式打架 | 生产四处全走 v2（capacity.py:91 / settings_api.py:1090 / runner.py:375 / cli.py:354），v3 零生产调用（仅 tests/test_planner.py） | §5 订正 |
| E2 容量函数签名缺 | `capacity_accounts(window_sec, gap=0, avg=None)`@97；`capacity_accounts_v3(window_sec, k=1, avg=None, bucket_rate=1.0, util=0.8)`@118 | §5 补 |
| E3 `YIBAN_SCHEDULER_V3` 当既有开关 | 全仓 0 命中；且无 `yiban/config.py`（配置在 schedule.planner_config / config_check.py） | §3.6 改为「待新建开关（默认 0）」 |
| F1 `_slot_to_bi` 被 planner 与 web 共用 | web 零引用（`grep -rn "_slot_to_bi" web/` 0 命中）；web `_pref_slots`（my.py:175-208）是同口径独立重实现 | §8 订正 |
| F2 删 v2 会让 planner 与 web 全断 | 仅 planner 断（planner.py:34 import / :253 调用） | §8 订正 |
| F3 `_slot_to_bi` 形态含糊 | schedule.py:380-398，签名 `_slot_to_bi(cfg)`，返回 `{偏移:块索引}`，确实不套 `window.bounds()` | §8 补 |
| F4 `_slot_to_label` 在 my.py:279 | 家在 accounts_data.py:316（app.py:1091 转发壳）；my.py:279/378/405、accounts_api.py:90 是调用点 | §8 订正 |
| F5 E9「自选片静默丢弃」 | 裁剪时返回空 dict → v2 `build_schedule`@478 / v3 `_place_prefs`@268 各 warning 后回退自动分配（不丢号，仅日志级 warning、无管理员告警/UI） | §8 改写 |
| F6 几何迁入的循环 import 风险 | `window.py` 叶子模块（21-22 只 import datetime/os），engine→window 单向依赖 | §8 补正面结论 |
| F7 注释 bug 未登记 | time_prefs.py:123 / accounts_data.py:317 的「06:30 → 390」与「窗口相对偏移」语义矛盾 | §8.2 订正 + 登记 T6 |
| F8 容量数字无出处 | 15 = `_DEFAULT_BLOCK_CAP`（schedule.py:31）/`YIBAN_BLOCK_CAP`(:199)；300 = `SLICE_SEC`:41/`SLOTS_PER_SLICE`:46/:335 | §8.3 补出处 |

总评：本次核验共订正 **30 处**，其中**实质 26 处、行号 4 处**；核心主张——v18 平移缺口、v3 容量公式零调用、`window.py` 叶子模块、几何中立化可行——全部成立。

---

## 明确缺陷

| 编号 | 缺陷 | 影响 | 处置 |
|------|------|------|------|
| D1 | FAILED 双层语义（不再自动重试、但可补签）是为消解 L3-3 与现行 has_undone 的表面矛盾而作的设计裁决，未见用户逐字确认 | 若用户本意「failed 完全终态」，needs_second_run 谓词要收窄，补签轮行为改变 | **2026-09-23 裁决：按现行为实施**（needs_second_run 含 FAILED，零行为变化优先）；改口径只动一处谓词 |
| D2 | 合并格 SKIPPED > FAILED 的次序是设计选择（管理决策强于失败猜测），反向亦有道理 | 跨源同 phone 一源 skipped 一源 failed 时结果取决于次序 | 概率极低（跨源同日分歧已被日写者消掉大半）；实施时以注释固化理由，争议再调 |
| D3 | 防「回滚后 v3 残骸进程」是领取边 + fencing 双拦，进程级防呆（如启动时查 writer 自杀）未设计 | 残骸进程持续空转打日志（写被拒但不退出） | C 阶演练时观察；需要则加一行启动自检（低风险小改） |
| D4 | 影子行（owner 前缀 `shadow:`）对 `latest_day` / 执行体归属的展示污染未逐处排查 | web 可能显示 shadow 归属 | A 阶实现 owners 口径时过滤前缀并补测试 |
| D5 | `yiban/status.py:26-28` 关于 `no_position` 的注释自称「不触发补签重跑」，但 `UNDONE_STATUSES`（status.py:67-71）**含** `STATUS_NO_POSITION`——注释与代码自相矛盾 | 按注释理解会误判补签轮行为 | **以代码为准**（保持现行「含 no_position」行为，A 阶不得顺手改行为）；注释矛盾待 M2 处置 |
| D6 | 未知状态串 → FAILED 的 fail-safe 映射与现行 `in UNDONE_STATUSES`（未知不算未了结）不同 | 未知串会被判为「未了结」，补签轮多跑一次 | 属**显式记录的安全向偏离**（宁多跑不漏签）；实施时以注释固化理由 |
| D7 | `sign-daily-<day>.json` 是**符号表而非状态表**（`yiban/engine/runner.py:538-560` 写成 `{phone: 符号}`，只含 5 种状态、无 `time` 字段），本文档 §3.3 曾并列引用它与 `sign-state` 作为 backfill 输入 | 若按旧文实施，`from_status_json` 会把符号当状态码解析、且拿不到 `run_at` 所需时刻 | **backfill 只读 `sign-state-<day>.json`**（§3.3 已订正）；缺该文件的日**跳过**，台账以两表为准 |

## 待办

| 编号 | 事项 | 处置时点 | 状态 |
|------|------|---------|------|
| T1 | D1 的 FAILED 谓词口径请用户拍板 | A 阶动手前 | 已裁决（按现行为，见 D1） |
| T2 | MF-2/MF-3（vshard 集中错配、density 单桶）登记 must-fix-list 并归 §12 批 | 即刻 | 已登记（MF-2 vshard 集中错配、MF-3 density 单桶） |
| T5 | 自选片映射迁移（§8）并入 M1 任务：工作流已停用，改为直接派发子代理；任务重排见 `docs/dev/m1-task-rearrangement-20260923.md`（任务 6=台账 A 阶、7=台账 B 阶、8=自选片映射迁移、9=executor_v3+分流、10=web 闸门+窃取+文档） | 已随工作流修订落地 | 已完成 |
| T3 | capacity_of **四处**调用点切换 + K 自动公式（§5）并入 M1 任务 7 验收 | M1 验收时 | 未开始 |
| T4 | 文档契约合规复核：本文档为 doc-writing-contract 首个试点，交付自检六项已过但「45 行头部」阈值待校准（见契约 D2） | M3 收尾 | 未开始 |
| T6 | §8 迁入时订正 `time_prefs.py:123` / `accounts_data.py:317` 的「06:30 → 390」注释（绝对分钟制写法与「窗口相对偏移」语义矛盾） | 任务 8 实施时 | 未开始 |
