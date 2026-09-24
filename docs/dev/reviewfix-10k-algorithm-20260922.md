# 万级签到调度：最终算法设计（2026-09-22）

> 目标：**N = 10000+ 账号，单窗口（有效 4680s）内各真实登录一次**，支持多种调度形式（顺序/随机/正态），
> 多执行体、越碰不到边界越好（上游有风控），且算法与数据库读写都撑得住。
> 本文综合：①本仓已有设计资产（`docs/refactor/43-scheduler-report-director-kworker.md` 的 Director+K 方案与
> 8000 人 DES 实测、`40-capacity-estimate.md` 的容量实测）②开源成熟算法调研（GCRA/token bucket、
> Netflix concurrency-limits、EDF、NHPP 采样、SWRR/DRR、water-filling）③用户建议的流程
> （**预检 → 单时间片内抢人 → 失败的 fallback**）。
> 所有引用来源在文末。

---

## 0. 结论先行

**最终形态（一条主链 + 三个正交控制）**：

```
                ┌── 预检（可行性 + 计划生成，一次性算完） ──────────────┐
                │  λ_needed = N/(W−R_retry) ；K_needed = ⌈λ·t/util⌉    │
                │  φ(t) 密度 + 自选片硬约束(water-filling) → 每账号目标时刻 │
                │  种子 = hash(phone, day) ⇒ 计划是纯函数、可重放、与 K 无关 │
                └───────────────────────┬───────────────────────────┘
                                        ▼
   ┌── 执行：时间片准入 + 片内抢人（唯一热路径） ────────────────────────┐
   │ 片 i 的准入预算 = φ_i × Δ（且 ≤ Λ）× 全局令牌（GCRA / token bucket） │
   │ 片内：所有 K 个执行体从**派生工作集**竞争领取（一条 SQL 原子 claim）  │
   │        顺序 = EDF（deadline = min(自选片末, W−R_retry)）              │
   │        控制 = 每账号 GCRA(T=gap, τ≈0) 防单账号过密                    │
   └───────────────────────┬───────────────────────────────────────────┘
                           ▼
   ┌── 兜底收敛（无独立补签轮） ─────────────────────────────────────────┐
   │ 未终态账号 → 退避重试（落点有界于 deadline）→ 跨片顺延（EDF 提权）    │
   │ 近截止 hedged（幂等 already 吸收双跑）｜终态单写 + fencing epoch      │
   └─────────────────────────────────────────────────────────────────────┘
   耦合面：所有可变状态（令牌、claim、终态）都在**同一个 SQLite**，一笔原子写搞定居留权与配额
```

**关键数字（N=10000、W_eff=4680s、t=1.87s、gap_account=10s）**：

| 项 | 现状模型 | 本设计 | 说明 |
|---|---|---|---|
| 需要执行体 K | **26**（10000/394） | **7**（λ=2.45 号/s × 1.87s ÷ 0.7） | 现状被 `周期=t+gap` 卡死；gap 移入全局速率后执行体只提供并发 |
| 聚合请求速率 | 无显式上限（K 倍放大） | `≤ Λ`（与 K 无关），本场景 ≈12.8 req/s | 与现状 K=20 部署的 ~10 req/s 同档，**风险不变** |
| 内存 | — | 7×40MB = **280MB**（生产 available 1352MB） | 40 号实测 RSS≈40MB/进程；**内存是硬约束**（生产无 swap） |
| CPU | — | 2.45×0.06 = **0.147 核**（≈7% 整机） | 40 号实测 0.06s/账号 |
| DB 写 | 每账号读改写整天 JSON ⇒ **O(N²)** | 约 **11 行/s**（5 万行/4680s，批量提交） | 见 §4；SQLite FULL 470/s、NORMAL 10.8k/s |
| 单账号间隔 | 每执行体各自 `avg+gap` | 每账号 GCRA + 全局令牌 | 语义等价（同账号一天仍只真实登录一次），但**不再浪费执行体** |

---

## 1. 与已有设计资产的关系（先别重复造轮子）

`43` 号文档已经做了三件对的事，本设计**继承**：

1. **算法选型有据**：HRW(rendezvous) 分配 + Power-of-Two-Choices + Work Stealing + Tail-at-Scale(hedged) + EDF + **每账号 CAS lease**。
2. **DES 实测**：8000 人、`sched_compare.py`、5 种子。结论：**K≥6 才能完成 8000**；"改良版"（按目标时刻 EDF + 自选片优先）把**自选命中从 6.5% 提到 84.8%**、**截止安全 100%**（简易版 60–80%）。
3. **最关键的一条洞见**（其 §4.2④，与本次独立推导一致）：**真正的容量天花板是 gap，不是并发**——"若跨账号最小间隔是全局硬约束，容量≈`W/gap`，加执行器无效"。

**本设计对 `43` 的两处修正**（有依据，非口味）：

- **不实现 Director / HRW / P2C / 显式偷取**。理由：`43` 自己指出"改共享池 + 竞争消费后，分配与转移大部分由池免费提供，自研算法只剩入队优先级与一个 CAS"。而**速率上限必须集中**（一个全局令牌行）⇒ 集中式分发反而多一层转发。**共享池 = 调度器**，EDF 排序 + 一条原子 claim 就是全部算法。
- **补一个 `43` 缺的一等公民：聚合速率**。`43` 把 gap 当"必保项"（§7），但 gap 的语义其实是"跨账号最小间隔"（**软约束**，用于人类相似度/风控），而平台真正在意的是**聚合请求速率**。把 gap 折进全局令牌后，二者都保住，且 K 从 26 降到 7。

（`43` 的阶段 B「Celery + RabbitMQ」在本设计里**不需要**：那解决的是多机/持久化/监控，而 10k 单机 7 进程 + SQLite 已经够，见 §4。）

---

## 2. 预检（Pre-check）：把"能不能跑完"在开跑前算清

**① 可行性（三步算术，全部来自 `40` 号实测标定）**

```
λ_needed = N / (W_eff − R_retry)          # R_retry = 重试储备（默认 600s）
K_needed = ceil(λ_needed × t / util)      # util = 目标利用率（默认 0.7）
K_mem    = floor((available_MB − 180) / 40)   # 生产无 swap ⇒ 180MB 是硬底线
K_cpu    = floor(0.9 × vCPU / (cpu_s_per_acct / t))
K        = min(K_needed, K_mem, K_cpu)
```
- 若 `K < K_needed` ⇒ **明确申报缺口**（"按当前窗口与上限，预计 k 个账号签不完"）+ 给选项：
  ①加宽窗口 ②降低 N ③提高 Λ（若实测平台能承受）。**绝不静默跳过**（这是现状 `skipped_window` 静默漏签的根因）。
- 若 `λ_needed > Λ`（平台天花板）⇒ 同上（此时**加执行体无用**——这是 `43` §4.2④ 的结论，要写进 UI 文案）。

**② 计划生成（一次性 O(N log N)，结果不落库）**

- **密度** `φ(t)`：窗口内目标到达密度（号/秒）。三种"调度形式"统一为 φ 的不同形状：
  - `sequence` → 确定性序列（φ 为均匀 + 稳定顺序）
  - `random` → 齐次泊松（φ 常数，但**用 NHPP 生成器**以保证 t=0 不 stampede）
  - `normal` → 截断正态（μ∈[40%,60%]、σ_eff 封顶）。**新增约束：`φ_max × N ≤ Λ`**——现状的正态只封了 σ，没封峰值速率，中段会形成相对突发。
- **自选片（硬约束）**：用 **water-filling / progressive filling** —— 自选账号先"封顶固定"，剩余自由质量在未被占用的时间轴上抬升公共水位（=统一密度）直到填满窗口。现状"片满先到先得 + 溢出就近顺延"只保证不超容，**不保证速率平坦**。
- **到达时刻**：`t_a = Λ⁻¹(u_a)`（累计强度反演，分段常数 φ 时是 O(1) 分段线性反演，**零浪费**；φ 复杂时才退化为 Lewis–Shedler thinning）。`u_a = hash(phone, day)` ⇒ **计划是 `(phone, date, 全局种子)` 的纯函数**：
  - 与 K 无关（加/减执行体不改计划）；
  - 可逐号重放（审计与排障）；
  - 杜绝 numpy 文档点名的反模式（把 worker_id 混进种子 ⇒ 重分片会改掉全站计划）。

---

## 3. 执行：时间片准入 + 片内抢人（唯一热路径）

### 3.1 为什么是"时间片"

- 时间片（默认 5 分钟，与现状 `_schedule_blocks` 同粒度）提供**有界的准入预算**：片 i 最多放行 `φ_i × Δ` 个账号 ⇒ 天然限制瞬时突发（**固定窗口的边界突发是研究点名的头号风控风险**，Nginx/Cloudflare 都专门讲过）。
- 片内允许**竞争消费**（抢人）：所有执行体从同一派生工作集里抢 ⇒ 无空闲、无预分配、无偷取代码。

### 3.2 派生工作集（不回放"计划快照"）

`A(t) = {生效账号 ∧ ¬user_paused ∧ 今日无终态结论}`，**每次循环重算**（一条带索引的 SQL）。
⇒ 窗口内新过审的账号**立即进入**（不再依赖兜底/补签轮/部署开关）；暂停/删除**立即退出**（零 claim、零跳过、零日志噪声）。这是 `round.py:294`"计划外立即入堆"这一类 bug 的结构性消除。

### 3.3 热路径：一条原子 SQL 同时取得"居留权 + 配额"

研究结论：**per-account CAS lease 与限速可以共用同一个原子写点**，不必引入 Redis。

```sql
-- 每账号每轮一次；BEGIN IMMEDIATE 内完成，读写同事务
BEGIN IMMEDIATE;
-- ① 全局令牌（GCRA/令牌桶单行）：不足则返回"等 t 秒"而不占用
UPDATE governor SET tat = MAX(tat, :now) + :T
 WHERE id=1 AND :now >= tat - :burst;
-- changes()=0 ⇒ 限速触发，回滚并 sleep(retry_after)
-- ② 每账号最小间隔（GCRA per-account，T=gap_account, τ≈0）
UPDATE sign_gcra SET next_ok_at = MAX(:now, next_ok_at) + :gap
 WHERE phone=:p AND :now >= next_ok_at - :tau;      -- changes()=0 ⇒ 该号太密
-- ③ 领取（CAS lease + fencing epoch）
UPDATE sign_claims SET owner=:worker, epoch=:epoch, state='claimed', heartbeat_at=:now
 WHERE phone=:p AND day=:day AND (state='pending' OR :reclaim);
COMMIT;
```
- **一个事务、三条 CAS、零锁等待**（SQLite 单写者天然串行⇒无需分布式锁；`busy_timeout=15s` 已在用，实测 K≤40 无 `locked`）。
- `epoch`（单调计数）做 **fencing**：过期执行体的终态写被拒 ⇒ 顺带解掉 T-CLAIMS-3 的时钟回拨覆盖。

### 3.4 排序：EDF（含重试提权）

- 主排序 key = **deadline** `d_a = min(自选片区间末, W_end − R_retry)`，`heapq` 维护。
- 重试项用 **laxity** 提权（`laxity = d_a − now − 剩余估计耗时`）——但 **LLF 只用于尾部救援决策，不做主排序**（研究：LLF 在过载下抖动，EDF 行为更可预测）。
- 可调度性**不用** `U≤1` 当保证（gap/lease/共享 DB 破坏了 EDF 的单处理器前提），只用它做量级自检；真值取自 `40` 号实测。

---

## 4. 兜底收敛：失败与未完成（**不再需要独立补签轮**）

- **退避重试**：失败分类（网络类 vs 凭据类；后者走现有 `cred_state` 熔断）→ 指数退避 + jitter，**落点有界于 `d_a`**，绝不越过窗口（消除"重试抽样落到窗口外 → 静默跳过"，即 T-WIN-1 那一类）。
- **跨片顺延**：片末仍未终态的账号进入下一片，按 EDF 优先（天然实现 `43` 的"尾部长尾保护"）。
- **近截止 hedged**（Tail-at-Scale）：设 `--lead` 前导秒，临近截止对卡壳账号垫一份备份执行——**因为 `already` 幂等**，双跑不会重复真实签到，但**要占速率预算**，故仅在令牌允许时启用。
- **终态单写**：每 `(账号, 日)` 恰好一条终态（含 `user_cancelled`/`paused`/`skipped_window` 等跳过类）；工作集因此单调收缩。
- ⇒ **补签轮退场**：它的两个职责（捉漏、首轮未收尾）分别被"连续工作集"与"守护重启"覆盖；`need_second_run` / `run.sh` 退出码 10 / `_is_second_run` 三个契约的迁移见上一份文档（含**发布门禁"有效轮次"定义需重新商量**这条治理性变更）。

---

## 5. 自适应（可选，窄带）

调研的三个坑里最相关的一个：**Netflix/Envoy 的延迟触顶信号在"近常量服务时间"下信噪比很差**（本项目 t≈1.87s 近常量）。所以：

- **保留离线标定值作初值**（`40`/`43` 的实测），只在 `[标定×0.7, K_mem 硬顶]` **窄带内**微调；
- 增长：AIMD 小幅上探；回退：**风控/429/验证码/超时/被拦页**一票乘性回退（把它们当"丢包"信号，比延迟信号可靠）；
- 收缩方向才平滑（Gradient 的 `smoothing` 只在降档生效），避免震荡；
- `max_limit` 用**内存**推（`(available−180)/40`），生产无 swap ⇒ OOM 是杀进程，不是变慢。

---

## 6. 数据库与存储：1 万账号下的读写账

### 6.1 现状的真瓶颈不是 SQLite，是**每账号状态文件**

现状：`_write_sign_state(phone, …)` = 读整天 JSON（1 万条 ≈ 1–2MB）→ 改一条 → 原子写回，**每账号一次** ⇒ 1 万次 × 读+写整份文件 ≈ **20–40 GB 的 I/O 放大**，且每次都持文件锁。这是万级**第一个会崩的地方**（不是 CPU、不是 SQLite）。

**改法（三选一，推荐第一）**：
1. **状态入库**：`sign_state(day, phone, status, message, scheduled, updated_at, epoch)`，PK `(day, phone)`，逐条 UPSERT = O(1)；
2. 分片文件（按 `hash(phone) % 16`），把 O(N²) 降到 O(N²/16)（治标）；
3. append-only 日志 + 定期快照（etcd WAL+snapshot / Kafka compaction 模式，最适合"只追加终态"的语义）。
   —— 若保 `/api/calendar` 等读侧契约，就在收尾时**一次性导出**成现有 JSON（导出是 O(N)，只做一次）。

### 6.2 写入量核算（N=10000）

| 写入项 | 行数 | 窗口内速率 |
|---|---|---|
| claim（领取） | 10k（+ 续租若干） | ≈2.1/s |
| 终态状态 | 10k | ≈2.1/s |
| 签到事件 `sign_events` | 10k × 2–4 | ≈4–9/s |
| 心跳（K=7，每 30s） | ≈1.1k | ≈0.2/s |
| **合计** | **≈50k 行** | **≈11/s** |

对照 `40` 号实测：SQLite `synchronous=FULL` ≈470 写/s、`NORMAL` ≈10.8k/s（本项目记忆中的实测值）⇒ **11/s 有两个数量级余量**。要点是**批量提交**（每 50 行或每 200ms 一个事务）+ **避免长事务**（`40` §5 约束 3 点名的 `replace_accounts` 整表重建）。

### 6.3 读路径

- 计划生成：一次全量账号快照（**只读明文列，不解密**——`capacity` 已有此口径）+ 自选片 + 昨日结果，三个带索引查询。
- **`/api/accounts` 全量解密是已知待办**（1 万行 AES-GCM 解密 + 分页）；万级部署前应改为分页解密/按需解密。

### 6.4 SQLite 参数与红线（沿用现状，明确保留）

`WAL` + `busy_timeout=15s` + 单写者批处理；**web 维持 `-w 1 --threads 8`**（内存态限速与审计链的红线，`40` §5 约束 4/5）；web 若要多 worker，**必须先把限速状态与审计写外置**——这条不是本设计引入的，是既有红线。

---

## 7. 验收不变量

1. **速率**：任意 1s 内实际请求 ≤ `min(φ, Λ)` 的积分 + burst；**与 K 无关**。
2. **分布**：给定 `(date, seed)`，未选片账号的到达时刻**逐号可重放**；自选账号 100% 落在其片区间内。
3. **收敛**：每个生效账号当日恰好一条终态；工作集单调收缩至空。
4. **恰好一次**：`(phone, day)` 唯一 + fencing epoch ⇒ 任意 K、任意时钟跳变都不重复真实登录。
5. **截止**：所有尝试（含重试、hedged）落在 `W` 内；做不到时**显式申报缺口**。
6. **可观测**：每账号 1 行结果 + 每片/每窗口 1 个摘要 + 指标（λ 实际 vs 计划、队列深、滞后、t 的 p50/p95、拥塞事件、K 利用率、缺口预测）。这套指标同时就是"容量在线学习"的输入（`102-capacity-self-fit-assessment.md` 想要的拟合数据）。

---

## 8. 迁移分阶（每步可独立上线、可回滚）

| 阶 | 内容 | 收益 | 风险 |
|---|---|---|---|
| **0** | 状态入库 + 批量提交（§6.1） | 消除 O(N²) I/O，**万级前置条件** | 读侧契约（日历/日志页）需并行导出 |
| **1** | 全局令牌 + 每账号 GCRA，gap 折进速率（§3.3） | **K 从 26 → 7**；聚合速率可控 | 需确认平台 gap 是"按账号软约束"（`43` §7 的待确认项） |
| **2** | 派生工作集 + 时间片准入（§3.2） | ①②（暂停复读/新号入轮）结构性消失；补签轮退场 | 需迁移三个契约（含发布门禁定义） |
| **3** | NHPP 反演 + water-filling（§2②） | 三种模式统一、自选命中 ~85%（`43` DES 已证）、峰值速率受 Λ 约束 | 计划从"落库"变"纯函数"，排障方式要配套（带种子重放） |
| **4** | 窄带自适应 + 指标（§5/§7） | 免手工标定、缺口可预警 | 研究点名：延迟信号弱 ⇒ 只在窄带内动，默认关闭 |

**每阶的验收**：用 `scripts/loadtest/` 既有工具（`concurrency_probe.py` 做 K 阶梯、`sched_compare.py` 做 DES 对比、`scale_driver.py` 采样）——**已有基准可直接回归**，不需要新的压测设施。

---

## 9. 风险清单

| 风险 | 依据 | 缓解 |
|---|---|---|
| **固定窗口的边界突发**（风控头号风险） | Nginx/Cloudflare 文档点名 | 时间片预算 + GCRA 的 `TAT=max(now,TAT)+T`（不累积额度） |
| 自适应延迟信号失效（t 近常量） | Netflix/Envoy 前提是"排队可观测" | 只用窄带 AIMD + 风控信号降档；保留离线标定 |
| 采样不确定化 ⇒ 重分片改全站计划 | numpy 文档点名 `root_seed+worker_id` 反模式 | 种子 = `hash(phone, day)`；计划是纯函数 |
| gap 的实际语义（全局硬约束 vs 按账号软约束）未确认 | `43` §7 待办 | **上线阶 1 前必须实测确认**；若是全局硬约束则 K 收益消失（容量≈W/gap），本设计仍需保留（它把这件事变显式） |
| 生产无 swap ⇒ OOM 即杀 | `40` §4.1 | `K_mem = (available−180)/40` 作硬顶，盯 `available` |
| 补签轮退场牵动发布门禁定义 | PROMPT.md §6.10 | 治理性变更，需用户确认（阶 2 前置） |

---

## 10. 系统/存储侧调研结论（外部权威源已核实，对本文档的补充与修正）

来源见 §10.4。**先说一条红线级发现**。

### 10.1 ⚠️ 红线：`try_claim` 的降级是 fail-open（多执行体下会重复真实登录）

`yiban/store/claims.py:125-130` 在库异常（锁超时/迁移期）时 **`except Exception → return True`**，语义是"当作没人在抢，放行"。单执行体时这是"降级保可用"，但**多执行体下两个执行体可同时放行同一账号 ⇒ 两次真实登录**，直接踩上游风控这条红线。
⇒ **必须改成 fail-closed：拒跑 + 告警**（先例：K8s CronJob 的"错过 >100 次就报错不静默启动"）。

### 10.2 修正：耐久性要分级——`synchronous=NORMAL` 会丢"最近提交"，与"恰好一次"冲突

SQLite 官方 pragma 文档原文：WAL 下 `NORMAL` "**loses durability**；a transaction committed in WAL mode with synchronous=NORMAL **might roll back** following a power loss or system crash"。
后果：**一次成功登录的终态可能在断电后回滚 ⇒ 恢复后系统认为未签 ⇒ 再登录一次 ⇒ 重复登录**。
⇒ 分级方案：`sign_events` 等可重建的事件行用 `NORMAL`；**`sign_claims` 的 claim / settle（决定"是否已登录"）在提交边界用 `FULL`**。写入量 11 行/s，付得起（FULL 470/s 有 40× 余量）。本文档 §6.4 原写"synchronous=NORMAL 待审计取舍"——**现按此结论定为分级**。

### 10.3 确认与被强化的部分

| 本文档的写法 | 外部核实结论 |
|---|---|
| §3.3 "一条原子 SQL 同时取得居留权与配额" | ✅ 正确。SQLite 无 `SKIP LOCKED`，其等价物就是**单写者串行下的条件 UPSERT + rowcount 判定**（你现有 `try_claim` 已符合官方语义：并发下只可能一个赢家）。PG 的 `SELECT ... FOR UPDATE SKIP LOCKED` 是跨机形态的对应物，现在**不需要**。 |
| §3.3 `epoch` fencing | ✅ 且**必须覆盖 settle**。Kleppmann：租约过期 + STW 停顿可长达数分钟，"**the fix is to include a fencing token with every write**"，且"**requires the storage server to take an active role in checking tokens, and rejecting any writes on which the token has gone backwards**"。⇒ 只给 claim 加 epoch 而不给 `settle/give_up` 加 `WHERE epoch=:epoch` = 等于没做。另：`settle` 被 fencing 拒时要**回读确认是否已有终态**，再决定告警（Stripe 幂等键语义）。 |
| §6.2 批量提交（50 行/200ms） | ✅ 官方口径更强：SQLite FAQ "**easily 50,000+ INSERT/s, but only a few dozen transactions per second**"；"surround multiple INSERT with BEGIN…COMMIT … time per insert is greatly reduced"。 |
| §3.2 派生工作集 + 计划时刻 | ✅ 强化：**计划时刻应入库成可查询列**，热路径取 `scheduled <= now AND 无终态`。Redis ZSET 延迟队列**不必引入**（且 `RPOPLPUSH` 自 6.2 起 deprecated；BullMQ 自述"不保证精确到点"）；Celery/Redis 的 ETA 任务因 `visibility_timeout` 会**重复执行**（官方文档自认），与红线冲突。 |
| §5 自适应窄带 + 风控信号降档 | ✅ 并被 Little 定律文章补强理由：**cron 是突发之源**（"cron jobs cluster the work around boundaries of minutes/seconds"），这正是时间片准入要治的；另注意 **timeout+retry 会让 λ 随 W 上升**（重试风暴），所以要给重试加 jitter 与**预算上限**。 |
| §4 兜底收敛 | ✅ 强化：K8s `activeDeadlineSeconds` 是"窗口硬截止优先于重试预算"的工业先例；**绝不让重试越过窗口**。 |
| §6.4 "避免长事务" | ✅ 且要知道**症状不是报错而是排队延迟**：`busy_timeout` 下写冲突表现为 **写事务排队 p95 上升**（你 loadtest README 自己写过这点）。⇒ 把"写事务排队 p50/p95"接进运行期指标。 |
| §6.1 状态文件 O(N²) | ✅ 并补一条：那个 **`file_lock` 比 SQLite 单写者更粗**——超时 30s 后**降级为进程内锁并告警**（`yiban/infra/locks.py`），即"每账号一次整文件 I/O 的临界区"+"降级后跨进程互斥失效"两重风险。 |

### 10.4 需要补进指标集的四项（外部源与本文档 §7 一致，另加）

- **写事务排队 p50/p95**、**`SQLITE_BUSY`/`database is locked` 次数**、**文件锁降级告警次数**——这三项是当前监控盲区，也是"隐藏拥塞点"。
- 每窗口摘要行（N/成功/失败/跳过、λ 实际、t 分位、拥塞事件）——错误预算/SLO 的输入。
- 依据：USE 方法（"for every resource, check utilization, saturation, and errors"）、Little's Law 容量口径、Azure 队列级负载均衡（"monitor queue depth… or shed work at the producer"；"autoscaling without bounding consumers' aggregate downstream rate only moves the overload to downstream"——**正是"加执行体不提吞吐"的权威表述**）。

### 10.5 来源（本次实际抓取）

- SQLite：WAL 单写者 https://sqlite.org/wal.html ｜何时用 https://sqlite.org/whentouse.html ｜批量 vs 事务 https://sqlite.org/faq.html ｜耐久性 pragma https://sqlite.org/pragma.html#pragma_synchronous
- Postgres：`SKIP LOCKED` https://www.postgresql.org/docs/current/sql-select.html ｜advisory lock https://www.postgresql.org/docs/current/explicit-locking.html ｜UPSERT 原子性 https://www.postgresql.org/docs/current/sql-insert.html ｜WAL+快照 PITR https://www.postgresql.org/docs/current/continuous-archiving.html
- 分布式锁与 fencing：Kleppmann https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html ｜Redis 官方锁页 https://redis.io/docs/latest/develop/clients/patterns/distributed-locks/ ｜Stripe 幂等键 https://stripe.com/blog/idempotency
- 队列：Redis `RPOPLPUSH`（deprecated）https://redis.io/docs/latest/commands/rpoplpush/ ｜RQ https://python-rq.org/docs/workers/ ｜Dramatiq https://dramatiq.io/guide.html ｜BullMQ 延迟 https://docs.bullmq.io/guide/jobs/delayed ｜SQS 延迟队列 https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-delay-queues.html ｜Celery Redis 后端（ETA 重投警告）https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html ｜Celery beat 单实例 https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html
- 调度器形态：Temporal 心跳/重试/Continue-As-New https://docs.temporal.io/encyclopedia/detecting-activity-failures 、https://docs.temporal.io/encyclopedia/retry-policies 、https://docs.temporal.io/workflow-execution/continue-as-new ｜DBOS https://docs.dbos.dev/ ｜K8s Job/CronJob https://kubernetes.io/docs/concepts/workloads/controllers/job/ 、https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/ ｜Airflow DAG run/backfill https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/dag-run.html 、https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/backfill.html
- 可观测/背压：Little's Law（Marc Brooker）https://brooker.co.za/blog/2018/06/20/littles-law.html ｜USE https://www.brendangregg.com/usemethod.html ｜Azure 队列级负载均衡 https://learn.microsoft.com/en-us/azure/architecture/patterns/queue-based-load-leveling ｜Envoy 熔断（含 retry budget）https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/circuit_breaking ｜gRPC 流控 https://grpc.io/docs/guides/flow-control/

### 10.6 修订后的"现在就该改"清单（6 项，低成本、防返工、防红线）

1. **`sign_claims` 加单调 `epoch` 列**，claim 自增；`settle/give_up` 的 `WHERE` 带上 `epoch=:epoch`（fencing 覆盖**所有**终态写）。schema 变更现在最便宜。
2. **`try_claim` 的库异常降级改 fail-closed**（拒跑 + 告警），消除"两执行体同时登录同一账号"。
3. **耐久性分级**：claim/settle 提交边界用 `FULL`，事件行用 `NORMAL`（防"断电回滚成功登录"导致的重复登录）。
4. **状态写路径抽象成可切换存储接口**（文件/DB 行），热路径支持批量 → 为阶 0（状态入库）留好接口。
5. **埋"写事务排队 p50/p95 + 锁降级次数"**（当前监控盲区）。
6. **重试加 jitter 与预算上限**（防 timeout+retry 放大 λ 的重试风暴）。

> 其中 1/2/3 与"是否做万级"无关——它们在**今天的两执行体生产形态下就已经是正确性问题**，建议并入下一批修复。

- GCRA / TAT / 突发容限：https://brandur.org/rate-limiting ，https://handwiki.org/wiki/Generic_cell_rate_algorithm ，https://github.com/brandur/redis-cell
- Token bucket / 漏桶 / Nginx limit_req / Cloudflare 近似滑动窗：https://handwiki.org/wiki/Token_bucket ，http://nginx.org/en/docs/http/ngx_http_limit_req_module.html ，https://blog.cloudflare.com/counting-things-a-lot-of-different-things/
- 分布式限速设计（Kong policy / Envoy 外部限流 / 局部限流表达变速率）：https://developer.konghq.com/plugins/rate-limiting/ ，https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/rate_limit_filter ，https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/local_rate_limit_filter
- 自适应并发（Vegas/Gradient/AIMD 与 Little 定律口径）：https://github.com/Netflix/concurrency-limits ，https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/adaptive_concurrency_filter
- EDF 最优性与 `U≤1`：https://handwiki.org/wiki/Earliest_deadline_first_scheduling
- Little 定律与排队：https://handwiki.org/wiki/Little%27s_law ，https://cran.r-project.org/web/packages/queueing/index.html
- NHPP 采样（反演/thinning）：https://handwiki.org/wiki/Poisson_point_process ，https://cran.r-project.org/web/packages/simEd/index.html
- 可复现随机种子：https://numpy.org/doc/stable/reference/random/parallel.html
- SWRR（Nginx 源码逐字）：https://github.com/nginx/nginx/blob/master/src/http/ngx_http_upstream_round_robin.c
- DRR / WFQ：https://man7.org/linux/man-pages/man8/tc-drr.8.html ，https://handwiki.org/wiki/Deficit_round_robin ，https://handwiki.org/wiki/Weighted_fair_queueing
- Water-filling：https://handwiki.org/wiki/Water-filling_algorithm
