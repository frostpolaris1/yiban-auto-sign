# 调度 v3 运维手册

面向部署与值守人员。调度 v3 是「计划 → 队列 → 领取 → 执行 → 收尾」的队列化签到形态，
以 `sign_tasks` 为唯一事实源，按 HRW 虚分片把账号分给并行执行体。本文只讲**怎么开、
怎么看、怎么救**；设计与公式的取舍见 `docs/dev/ledger-dual-version-design-20260923.md`、
`docs/dev/upgrade-data-migration-design-20260923.md` 与
`docs/dev/m1-task-rearrangement-20260923.md`。

> **两条运维纪律**（先读这两条，再读全文）：
> 1. **当 Λ（全局速率上界）存在时，加出口不再线性放大总速率**——`Σ出口速率` 被
>    `min(φ, Λ)` 封顶。**不要用"加进程"解吞吐问题**：扩容首先是加出口（`YIBAN_EXECUTORS`
>    的出口行），加执行体进程只增加并发通道、不放大总量。
> 2. **gap 安全件的默认开关「待实测裁决」**——`YIBAN_ACCOUNT_GAP_MAX` 缺省 10s、
>    `YIBAN_ACCOUNT_GAP_ENFORCE` 缺省 1=开（即默认启用）。上游风控究竟按「账号间隔」还是
>    按「出口/IP 速率」计数尚未实测，故这个安全件**先默认开着**；`gap` 本身不可移除，
>    要关只在实测证明"账号维度不是风控面"之后，由管理员显式改键。

---

## 1. 开关与灰度

调度 v3 由 `YIBAN_SCHEDULER_V3` 单键分派，**缺省 0**（未设 / `0` / `false` / 手写错值
一律走旧的 v2 路径）。**开关即回滚**：出问题把键改回 `0`，下一轮就是旧路径，无需改库、
无需回退版本。

| 键 | 含义 | 缺省 | 何时改 | 回滚 |
|----|------|------|--------|------|
| `YIBAN_SCHEDULER_V3` | 调度 v3 总开关（真值 `1/true/on/yes`） | `0`（关） | 灰度开闸 | 改回 `0` |
| `YIBAN_EGRESS_RATE` | 每出口令牌桶速率（attempt/s） | `1.0` | 换出口/实测定档；**显式写入即人工接管，AIMD 停止上探** | 清空该键回出厂速率 |
| `YIBAN_GLOBAL_RATE` | 全局聚合速率上界 Λ（attempt/s） | 空=不限 | 站点级风控压总速 | 清空该键 |
| `YIBAN_ACCOUNT_GAP_MAX` | 每账号最小间隔（秒） | `10` | 风控收紧/放宽 | 改回 `10` |
| `YIBAN_ACCOUNT_GAP_ENFORCE` | gap 安全件开关 | `1`（开） | **待实测裁决**（见纪律 2） | 设 `0` |
| `YIBAN_EXECUTORS` | 执行体清单（含出口/停用行） | 空=单执行体 | 增删出口/执行体 | 删清单回旧三键 |
| `YIBAN_WORKERS` | 并行执行体数（人工优先） | 空 | 与清单同改 | 清空 |

**开闸前置清单**（摘 `docs/dev/m1-task-rearrangement-20260923.md` §7 / R-T14，逐条确认
才开闸）：

1. **失败通知已补齐**：v3 的最终放弃会发管理员条目与用户失败邮件，与 v2 对齐；
   `_alert_slow_sign`（慢签到告警）仍未接入，属已知缺口（见 §7）。
2. **当日单写者**：v3 **不写** `sign_claims`，只写 `sign_tasks`。同一天若先跑过 v2
   （旧表里有当日行）再切 v3，`has_undone_accounts_today` 读旧池会误判补签闸门。
   **开闸当日只允许一个写者**（要么整天 v2，要么整天 v3）；纯 v3 日旧池 `total==0`，
   回退读 `sign-state-*.json`（v3 会写）语义正确。
3. **开闸日必须晚于 v18 平移 / v20 补账日**：`write_plan` 是 `INSERT OR IGNORE` +
   主键 `(phone, day)`，当日已有的 `vshard=-1` 历史行会挡住补建。真实后果很窄——
   升级当日已有终态（尤其 `failed`）的账号在该日不会再被 v3 重试（一次性、升级窗口内），
   但**错开日**即可完全避免。开闸最好与平移/补账错开一天。
4. 模块规模门禁已按实际行数重算（见 `tests/test_module_size_gate.py` 的登记项）。
5. 执行体页可观测性已补：v3 写**文件心跳**，`/api/scheduler/executors*` 的四态对 v3 成立
   （不再显示 `idle`）。
6. 影子期无影子行（`dry_run` 有意不落库）：只能靠 `planner.plan_stats` 的 `hist` /
   `peak_per_sec` 与现网落点对账（见 §6）。
7. `planner.py` 头部注释里"无计划则降级"的旧说法已过时（实际是补建计划），属注释流。
8. `attempts.RISK_FAIL_KEYWORDS` 与 `security.WAF_KEYWORDS` 是两份独立维护的同义列表，
   靠注释对齐；合并归后续注释批。

**灰度步骤建议**：先 `dry_run` 影子期 3 天对账落点分布 → 在**非平移/补账日**、
**当日无 v2 写入**的前提下把 `YIBAN_SCHEDULER_V3=1` → 当日盯 §6 的观测项与 §8 的排障项。

---

## 2. 架构一图流

```
账号清单
   │
   ▼
Planner（planner.build_plan）
   │  按 (phone, day) HRW 算 vshard，按窗口/模式算 run_at
   │  首次建计划时把当日虚分片数 V 落库（app_meta 键 scheduler_v3_v_<day>）
   ▼
sign_tasks（唯一事实源：state / vshard / owner / run_at / epoch / lease_until）
   │
   ▼
claim_batch(owner, day, 我的分片集)      ← 只取 state='pending' 且 run_at<=now
   │  单条 UPDATE ... RETURNING（写者串行下原子），置 claimed + 写 owner + epoch+1
   ▼
lane（M 条 asyncio 通道）
   │  等到 run_at → 出口令牌桶 → 全局 Λ → 每账号 gap 门 → 线程池执行 attempt_signin
   ▼
settle_tasks(owner, day, 结果, epochs={phone: 领取时的 epoch})
      批量收尾（单事务）：done / failed；被接管（owner 或 epoch 不符）的行写不进去
```

**HRW 分工**：`hrw.vshard_of(phone, day, V)` 把账号钉进 `0..V-1` 之一；
`hrw.shards_of(executor, executors, day, V)` 给出该执行体名下的分片集。执行体只
`WHERE vshard IN (自己的分片集)` 领取，日常零竞争。`V` 由账号规模选定
（`hrw.v_for`：<500→64、<3000→128、否则 256），**首次建计划时落库、此后只读**——
每轮重算会让同日删号后已写行的索引落到扫描范围外，那些行**永远不会被领取**。

**V 落库**：`app_meta` 的 `scheduler_v3_v_<day>`。落库值缺失、或已写行的最大分片号
不小于它（说明有行在范围外）时，按最大分片号放宽并留 error——放宽不会改变既有索引的
归属（`hrw.owner_of` 与 V 无关），只把扫描范围撑到盖住已写行。

**崩溃恢复**（本轮补齐）：**起跑**按「回收 → 死主接管 → 首轮领取」的顺序做一次
`queue_store.reap_expired`（租约过期**并超过宽限期**后回收本业务日的 `claimed` 行）
与死主分片接管，此后补货循环按 `RECOVER_SEC`（60s）周期重复这两件事。起跑必须接管：
补货循环那次按 `RECOVER_SEC` 节流，而短轮次干完自己的活即收干退出，等不到 60s，死主
分片的 `pending` 行会整轮无人领（补签轮沿用同一分片划分仍无人领 ⇒ 静默漏签）。执行体
起跑/存活期/收尾写文件心跳（见 §6）。

---

## 3. 容量与限速

**单位**：容量与速率的单位都是**账号尝试/s（attempt/s）**，不是 HTTP 请求/s。
单账号 = 登录链 4 + 定位 + 签到 = **6 次 HTTP 请求**，故 1 attempt/s ≈ 6 请求/s。
报数字时必须带这个单位，否则会把"尝试"读成"请求"。

**两套容量口径**（`schedule.capacity_of` 按 `YIBAN_SCHEDULER_V3` 分派，四处调用点统一
走它：web 保存闸门、现场实测换算、引擎预检、CLI `capacity`）：

- **v2（开关关，逐字不变）**：`capacity_accounts(W, gap, avg) = (W−avg)//(avg+gap)+1`。
  单账号周期 = `avg + gap`（gap 是「上一次完成 → 下一次开始」的下限）。这是**串行**口径，
  容量随窗口线性、与出口数无关。
- **v3（开关开）**：`capacity_accounts_v3(W, k, avg, bucket, util) = k × min(M/avg, bucket) × W × util`。

**M（通道数）**：`M = min(16, ceil(bucket_rate × avg × 2))`（`schedule.channel_count`，
唯一口径）。通道能力 `M/avg` 只需略高于出口桶上限，**瓶颈是 `M/avg` 与 `bucket_rate` 的
较小者**；只按通道吞吐算、忽略桶封顶会把容量高估约 2.3 倍。

**K（执行体数）**：`K = clamp(ceil(N×(1+r)/(W×bucket×0.8)), 1, 出口数)`
（`schedule.executor_count`，唯一口径）。`r` 是期望重试占比（缺省 0.2，依据分级重试预算
`attempts.MAX_ATTEMPTS=3` 等），`util=0.8` 是重试与尾延迟降额。**K 至多不超过出口数**：
再加执行体也只共享同一批出口（纪律 1）。

**限速三件**（都在 `yiban/engine/token_bucket.py`）：

- **出口令牌桶（GCRA）**：每出口一个速率（attempt/s），AIMD 自适应——无风控连续
  `SUCCESS_STREAK=200` 次后 `rate ×= 1.2`（封顶 `RATE_MAX=4.0`）；风控信号
  （e003 / WAF / 验证码 / 被拦页）`rate ÷= 2`（下限 `RATE_MIN=0.2`）并半开
  `HALF_OPEN_SEC=300`。**显式写了 `YIBAN_EGRESS_RATE` 即人工接管**：上探变 no-op，
  但风控的乘性回退照做（安全反应不随接管停）。桶状态落 `egress_state` 表（10s 粒度），
  重启不"重启即全速"。
- **全局上界 Λ**：`YIBAN_GLOBAL_RATE`（attempt/s，缺省空=不限）。所有出口共享的总量
  上界，与出口数 K 无关。
- **gap 门**：每账号两次尝试的最小间隔（`YIBAN_ACCOUNT_GAP_MAX` 缺省 10s），
  缺省开、不可摘除（纪律 2）。

---

## 4. 数据与状态

**`sign_tasks` 状态机**：

| state | 含义 | 谁能动它 |
|-------|------|----------|
| `pending` | 待办（未到点/被重排/租约过期并超过宽限期后被回收） | `claim_batch` 领取；`requeue_task` 重排；`reap_expired` 回收 |
| `claimed` | 已被某执行体持有（带 `owner`/`lease_until`/`epoch`） | `settle_tasks` 收尾；租约过期**并超过宽限期**后由 `reap_expired` 回退 |
| `done` | 了结（签到成功/已签/今日无任务） | 终态 |
| `failed` | 了结（失败/跳过类终态） | 终态 |
| `skipped` | 了结（窗口外跳过） | 终态 |
| `stolen` | 未了结（被接管标记） | 保留字 |

了结态集合是 `yiban.status.TASKS_SETTLED_STATES` 的**同一对象**（`done`+`skipped`）。

**`vshard = -1` 惰性行**：v18 平移与 v20 补账写入的行不属于任何分片集
（`hrw.shards_of` 只产出 `0..V-1`）。它们既不被 `claim_batch` 领取，也不被分片级接管，
`reap_expired` 也**必须排除**（回收成 `pending` 只会变成永不被领的行）。这是"升级不会
导致重复真实登录"的关键性质。

**epoch fencing**：每次领取 `epoch = epoch + 1`，收尾时把领取时的 epoch 原样传回
`settle_tasks(..., epochs=...)`。被接管/被回收的行 epoch 已变，原持有者迟到的收尾
**写不进去**（`WHERE ... AND (? IS NULL OR epoch=?)`），不会覆盖接手者的结论。
`requeue_task` 同样支持按 epoch 拒绝被接管者的重排。

**两个口径坑**：

- `day_counts(day)["open"]` 是**全表**统计（含 `vshard=-1` 的历史 `failed`），
  **不能当"当日是否了结"的闸门**——用它该日永远不了结（补签轮反复空跑）。
- 判"当日是否了结"必须用 `pending_count(day, 我的分片集)`：带 `vshard >= 0` 与分片集
  过滤，历史行天然不在其中。

---

## 5. 运维操作

```bash
# 开闸 / 回滚（.env；改完下一轮生效）
YIBAN_SCHEDULER_V3=1     # 开
YIBAN_SCHEDULER_V3=0     # 回滚（无需改库）

# 对账（迁移与双跑的验收门；exit 0=平、1=有差异、2=无法定论）
python scripts/ledger_check.py --day 2026-09-23 [--all-days 7]

# 巡检：容量建议与执行体数（只读，不联网）
python -m yiban.cli capacity --json
python -m yiban.cli config --json

# 备份门（任何改库/迁移前先备份；脚本内含一致性快照 + 加密 + 保留策略）
bash scripts/backup.sh
```

**运维操作纪律**：

- 开闸当日**单一写者**（见 §1 前置清单第 2 条）；回滚当天不要在同一日混跑 v2/v3。
- 改执行体清单/出口后**不自动重启进程**，下一轮定时任务或容器重启才生效；页面上的
  执行体行状态以 `/api/scheduler/executors*` 的四态为准，不以"清单写了什么"为准。
- **对账不过先别开闸**：`ledger_check` 非 0 时按 §8 处置。

---

## 6. 观测

- **日志**：轮次横幅含版本号与账号数；v3 起跑打 `v3 执行体：M 条通道 / N 个分片 / 出口 X`；
  接管打 `接管心跳过期的执行体 <peer> 的分片集，N 条待办改归本执行体`；回收失败、
  领取失败、收尾失败各有 warning。**日志里的手机号一律脱敏**。
- **事件**：每次尝试经 `event_sink` 落 `sign_events`（`stage='sign'`，含 `attempt`/`dur_sec`），
  任务结束后单事务批量落库。
- **状态文件**：`sign-state-<day>.json` 是网页日历的事实源（每次尝试与重试入队即写，
  故不会空窗）；`sched-run-<day>.json` 是全量收尾标记。
- **文件心跳与执行体页**：`worker-alive-<slot>.json`（v3 与监督进程都写）。四态：
  `running`（心跳新鲜，`now-ts <= 2×30s`）/ `finished`（有收尾标记）/ `idle`（当日无记录）
  / `stale`（有开始、无收尾且心跳过期 ⇒ 异常，成因含被强杀 / 超时 / 内部异常）。v3
  起跑/存活期/正常收尾都写，长轮次不会因心跳不刷新被判成 `stale`；被强杀或内部未预期
  异常（`run_executor_v3` 返回 `{}` 那档）不写收尾，留"有开始、无收尾"判 `stale`。
- **影子期对比口径**：`dry_run` 只算计划与落点分布、零落库零请求。用
  `planner.plan_stats` 的 `hist`（按有效窗口 5 分钟格的落点直方图）与 `peak_per_sec` /
  `lam` 跟现网落点对账；**影子期没有 `shadow:` 影子行**，别去库里找。

---

## 7. 已知限制与未做

- **三库分离**：`sign_tasks` 仍与业务表同库同连接（`_queue_conn` 是唯一取点，将来可切）。
- **§12 自适应体系**：vshard 集中校验、density 单桶口径等未做。
- **v2 退役 / `sign_claims` 冻结**：`sign_claims` 仍在只读过渡期，`round.run_queue_retry`
  仍写它；v2 未退役。
- **`outcome_buffer` / `shadow:` 影子行 / `ledger_days` 日写者表**：未引入。
- **`downgrade_all` 的恢复入口**：站点级熔断的"恢复"入口未接（降档入口在
  `token_bucket.downgrade_all`，另批处理）。
- **`_alert_slow_sign`（慢签到告警）**：v3 未接入（v2 有）。
- **K 的自动公式**只用于引擎预检；web 保存闸门/CLI/实测换算按"每执行体"（`k=1`）口径，
  v3 下**总容量 ≈ 该值 × 出口数**。
- **gap 默认开关**待实测裁决（纪律 2）；上游风控按账号还是按出口计数**尚未验证**。

---

## 8. 排障手册

| 现象 | 判定 | 处置 |
|------|------|------|
| 某些账号当天一直不签，库里是 `claimed` 且 `lease_until` 已过 | 崩溃/被杀的通道留下的行，未被回收 | 正常应在 `租约 60s + 宽限 120s + 回收间隔 60s`（最坏约 4 分钟）内回收；宽限期存在是因为**租约到期 ≠ 持有者已死**（慢尝试可能比租约还长，立即回收会让同一账号被重领、重复真实登录）。若仍卡住，查 `reap_expired` 是否报 warning（库异常），必要时手工跑一轮或重启执行体 |
| 页面执行体行显示 `stale` | 有开始、无收尾（心跳过期）：被强杀 / 超时 / 内部异常（v3 只在正常返回路径写收尾） | 查进程与宿主 `timeout`、日志里的"v3 执行体未预期异常"；长轮次若仍 `stale` 说明心跳刷新没走（补货循环未运行） |
| 页面执行体行一直 `idle` | 当日无该槽位心跳：v3 未起跑或心跳写失败 | 确认 `YIBAN_SCHEDULER_V3=1` 且本轮真的起跑；看日志有无心跳写失败 debug |
| 某日闸门永不了结、补签轮反复空跑 | 用 `day_counts` 的 `open` 当闸门，把 `vshard=-1` 历史行算进去了 | 改用 `pending_count(day, 分片集)`；历史行按设计保持原样 |
| 网页日历空窗（当天没记录） | 尝试未物化状态 | v3 每次尝试结束即写 `sign-state`；若空窗查状态目录权限与库 |
| V 不一致 / 有行没被领取 | 落库 V 与已写行最大分片号不符 | 日志会有"虚分片数不可用"error，按最大分片号放宽；核对 `app_meta` 的 `scheduler_v3_v_<day>` 与队列库 |
| 开闸后旧池/补签判定错乱 | 同日先跑过 v2（旧表有当日行） | 当日单写者规则（§1）；回滚或次日再开 |
| 容量显示为 0 或明显偏小 | 窗口退化 / 裁剪吃空 / 单位混淆 | 窗口退化会回退默认窗口（不应为 0）；核对单位是 attempt/s、`k` 是每执行体还是总数 |
| 吞吐上不去，加进程无效 | Λ 存在时总量被封顶（纪律 1） | 先加**出口**（`YIBAN_EXECUTORS` 行），再看 `YIBAN_GLOBAL_RATE` 是否该调 |
