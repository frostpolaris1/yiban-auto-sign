# 租约截止、回收冷却与会话标记（实验报告）

> 分支 `fix-b1-idempotency`（B1 栈，基线 `035444a1a`；本文主体由 #404 的 `fix/lease-deadline-guard` 带入）。
> 对象是新栈（`server/`，Go）调度域：`core/schedule` + `adapters/pg` + `app/worker`。
> 本文的行号按本分支 HEAD 记；基线坐标见 §1 的备注。
> 记录方式：先给代码坐标，再给实测数字，最后给生产参数建议。
> 所有数字都由本仓真库 e2e 现跑得出，复现命令见 §5。

## 0. 结论（四句）

1. **租约只回答"谁在跑"，不回答"还活着吗"。** 回收把行从 `running` 改回 `pending` 的那一刻，
   库层唯一的门（`uq_sign_tasks_one_running`，只盖 `state='running'`）就消失了：
   同一账号可以有两条签到链同时在飞（矩阵峰值并发 2），而且**旧持有者的成功结算会被
   `claim_seq` 栅栏拒掉**（矩阵 `fence=1`）——那次提交其实成功了，宿主侧没有任何落点。
2. **"一次尝试的总截止时间"（A）是唯一能在"持有者还活着"的前提下收住重复的改动**：
   预算 < 租约 ⇒ 行不进入过期态 ⇒ 回收 0 次、重复提交 0 次。它依赖提供方尊重 `ctx`；
   提供方吞掉取消时 A 完全失效（矩阵最后一格 `dup=1`）。
3. **"回收冷却"（B）单独用不是修复**：实测它把"并发重复"换成"**延迟重复 + 丢掉第一次成功**"。
   第二幕等到冷却结束再跑一轮，同一行被重新认领并再提交一次（`late=2`）。
   它作为独立保险有价值（挡住回收抖动与热循环），但不能替代 A。
4. **A 是默认开启的行为变更**（`YIBAN_ATTEMPT_BUDGET_SEC=0` → 派生 `0.8×120s=96s`）：
   过去 96–120s 才返回的慢提交原本 `success`，上线后会被切成 `transient`，三次后 `failed`。
   上线前必须知道这一条，见 §4 的"上线注意"。

## 1. 代码坐标：为什么会双跑

| 位置 | 事实 |
|------|------|
| `server/internal/adapters/pg/schedule.go:526` `ClaimDue` | 认领时写 `lease_expires_at = now + lease`，租约缺省 120s（`YIBAN_TASK_LEASE_SEC`，域 30..3600） |
| `server/internal/adapters/pg/schedule.go:679` `ReclaimExpired` | 租约过期就把 `running` 放回 `pending`；落点 `run_at = now + grace`（`schedule.go:698`，改动后） |
| `server/migrations/postgres/00012_scheduling.sql` | `uq_sign_tasks_one_running ON sign_tasks(account_id) WHERE state='running'` —— 只保护"正在跑"的那一行 |
| `server/migrations/postgres/00020_schedule_lease.sql` | `claim_seq` 递增 + `lease_expires_at`：认领栅栏，结算必须带本次序号 |
| `server/internal/adapters/pg/schedule.go:225` `Finish` | `UPDATE ... WHERE state='running' AND claim_seq=$6`，0 行即 `ErrScheduleConflict` |
| `server/internal/app/worker/worker.go:837` `attemptID` | `"<taskID>#<attempts>.<claim_seq>"`（一次认领周期一个键，见 `attempt-key-claim-cycle.md`） |
| `server/internal/app/worker/worker.go:424` `Sign` | 改动前直接吃 loop 的 `ctx`：一次提交可以无限期占着租约（现吃 `attemptCtx`，`:342`） |

> 基线 `fab09cff4` 上行号是 `ClaimDue:503`、`ReclaimExpired:653`、回收落点参数 `:699`；本文按改动后的 HEAD 记。

两条后果，本次实验都测到了：

- **(a) 并发双跑。** 租约到期 → 回收 → 第二个执行体认领同一账号 → 两次真实提交同时在空中（`peak=2`）。
- **(b) 迟到的成功没有落点。** 回收把行改成 `pending` 之后，原持有者的结算必然撞
  `Finish` 的 `state='running'` 判据（`fence=1`）；它那次**提交成功的结论被丢弃**，
  行留在 `pending`，冷却/退避过后被重新认领、**再提交一次**（矩阵第 5 格第二幕 `late=2`）。
  于是"回收冷却"单独上线时，重复并没有消失，只是从并发变成串行。

## 2. 三项改动

### A. 一次尝试的总截止时间（`YIBAN_ATTEMPT_BUDGET_SEC`）

- 端口：`appworker.Deps.AttemptBudget time.Duration`（`repo.go`）。
  端口层 **0 = 不限尝试时长**（不建立预算截止，保留引入预算前的行为）；
  它仍会在认领时租约已不足以结算（余量 ≤ 1s）时走 `wait_lease_spent`，
  但不给运行时长封顶。线上装配不传 0：
  `assembly.workerAttemptBudget` 在配置为 0 时派生 `0.8 × 租约`（`assembly.go:1250`）——
  也就是说这项改动默认生效。显式配置才是"改数值"，不是"打开开关"。
- 租约余量封顶：`ClaimDue` 在认领行上带出 `lease_expires_at`
  （`core/schedule/schedule.go` 的 `TaskRow.LeaseExpiresAt`、`adapters/pg/schedule.go`）；
  `worker.effectiveAttemptBudget` 把配置预算压到
  `min(配置, lease_expires_at - attemptStart - (settlementTimeout + 200ms))`。
  预算从凭据就绪起算、租约从认领起算，这段前序（暂停门/窗口/出口/限速/凭据解密）
  因此自动从预算里扣掉；余量不足以覆盖结算保留时 `effectiveAttemptBudget` 返回 `leaseSpent=true`，
  `execute` 以 `wait_lease_spent` 退回队列——**一个平台请求都不发，也不计 attempts**。
- 执行：`worker.go:320` 取 `attemptStart`，`:342` 建 `attemptCtx, cancel := attemptDeadline(ctx, budget)`；
  `DiscoverTargets`（`:349`）与 `Sign`（`:424`）吃 `attemptCtx`；
  结算仍走 `settlementContext`（`WithoutCancel` + 800ms），**预算截断不影响落库**。
- 判据：`attemptBudgetExhausted(attemptCtx, parent)`（`:642`）= 子 ctx `DeadlineExceeded`
  **且父 ctx 未结束**。父 ctx 退出是 `Canceled`（停机），不是"这次尝试失败"，不能冒充；
  调用方自己传进来的 `DeadlineExceeded`（如 HTTP 请求超时）同理不算预算截断。
- 结论词：`transient`，日志 reason `attempt_budget_exceeded`（`worker.go:68-72`）。
  必须与基础设施故障分开：后者上抛退回队列、**不计尝试数**；前者是一次真实尝试的失败，要计数，
  否则卡死的行会每轮重撞（第 3 次截断直接 `failed`，见 `TestAttemptBudgetExhaustedFinishesFailed`）。
- 边界（三条，读结论时必须一起看）：
  1. **A 只限时长，不防重**：截断只切断等待，已经发出去的请求是否生效由平台决定；
     对"吞掉取消"的提供方 A 毫无作用（矩阵第 7 格 `dup=1`）。
  2. **A 的释放路径绕过 B**：预算截断经 `settleOutcome → requeueRetry`（`worker.go:558`）
     直接置 `pending` + `run_at`，**不经** `ReclaimExpired` 的冷却。对吞取消的提供方，
     第二次真实提交因此比"等租约到期再回收"更早（96s vs 120s），且没有冷却。
  3. **预算不覆盖本地阶段**：`Locations.Resolve`、`Photos.PickForAttempt` 不在 `attemptCtx` 上，
     它们是本地/内存路径（无网络），但也确实不受这个预算约束。

### B. 回收冷却（`YIBAN_RECLAIM_GRACE_SEC`）

- `pg.ScheduleRepo.WithReclaimGrace(d)`，`ReclaimExpired` 落点改为
  `run_at = now + make_interval(secs => grace)`（`schedule.go:698`）。
- 审计：`task.reclaim` 的 detail 增 `grace_sec`（当时生效值，`schedule.go:737`）。配置会变，
  事后判"回收后多久被重领"必须看当时值；`core/audit` 的 detail 键白名单同步增列
  （`chain.go:363-366`），否则整笔事务回滚。
- `ResetRunning`（启动回收）**故意不加冷却**：它回收的是崩溃遗留行，原持有者已不在，
  不存在慢提交竞争。
- **定位（这条容易写错）**：回收已经把行置成 `pending`，滑动窗口里没有"行还是 running、
  慢持有者还能写终态"这种状态——慢持有者的 `Finish` 必然被 `claim_seq` 栅栏拒掉。
  所以冷却**不能**给迟到的结算"留落点"，它只是**推迟下一次认领**：把并发重复降级为
  **串行重复**，把"回收→重领"的间隔拉长到便于观测、顺便挡住回收抖动与热循环。
  是可观测性缓冲，不是止损。

### C. `Policy.ClearSession` → `Policy.SessionTainted`

- 原字段名声称一个**没有人执行**的动作：`grep '\.ClearSession' server/internal` 在
  `core/retry` 与 `signing/conformance` 之外零命中，`settleOutcome`/`finishFailed` 都不读它。
- 但它不能删：`signing/conformance/retry_vocab_test.go` 用 `MaxAttempts==1 && SessionTainted`
  当 **default 档哨兵**，用它检测 `BudgetFor` 是否有词没有显式分支。
- 处理：改名 `SessionTainted`（分类标记：会话不可信，清会话归提供方执行面——yiban 拿到 999 自清），
  所有分支与哨兵同步。价值是让名字不再承诺一个不存在的执行者。
- **不要和 `contract.RetryAdvice.ClearSession` 混为一谈**：那是另一条链上的字段
  （provider 填，conformance 校验，worker 不读，见 §6 顺带发现 1）。

## 3. 量化实验：租约双跑矩阵

装置（`server/cmd/server/lease_double_e2e_test.go`，真库 + 真 worker，独立 schema）：

- 一个账号、一行计划、一个窗口（`now-10m .. now+10m`）、两个 `appworker.Service` 实例，
  各自一份 `ScheduleRepo`（租约/冷却不同）。
- 被观察的 `Sign` 睡 `hold = 租约 + 800ms`：`ignoreCtx=false` 时 `select{time.After, ctx.Done()}`，
  `true` 时纯 `time.Sleep`（模拟提供方自带 HTTP 重试把取消吞掉）。
  时长由租约派生的原因：给"W2 的 `Step` 被调度推迟"留出余量，避免退化成"W1 先结算、于是没有重复提交"。
- 时序：W1 起 goroutine 进 `Step` → 等它真进入 `Sign` → 睡到 `secondAt` 再同步跑 W2.`Step` →
  收回 W1。`secondAt` 缺省 `租约 + 100ms`（模拟生产"下一个 tick"），
  "仅长租约"格显式设成 `450ms`（在租约之内）：它要证明的是"租约没过期就没人能领"，
  而不是靠回收/冷却挡住第二次认领——否则这一格会因为新时序被回收掉，退化成基线。
- 全程真时钟：冻结时钟会把要观察的现象消灭掉。

判据：`signs` = `Sign` 调用总数；`dup = signs-1`；`peak` = 同时在空中的 `Sign` 数；
`distinct` = 不同 `attemptID` 数（键 = `attempts.claim_seq`，故**有回收时必然 ≥ 2**）；`reclaim` = `audit_logs` 里 `task.reclaim` 行数；
`fence` = 两轮 `Step` 返回 `conflict` 的次数；`attempts`/`state`/`result` 取行上终态；
`late` = 第二幕（等冷却结束再跑一轮）之后的 `Sign` 总数。

**每一格都把表里的数字钉成断言**（`labCell.wantPeak/wantDistinct/wantReclaims/wantFences/wantAttempts/wantLateSigns`），
矩阵表不是日志摘录，而是被测代码的守护。

| 格 | 租约 | 预算 | 冷却 | signs | dup | peak | distinct | reclaim | fence | attempts | 终态 | late | 耗时 |
|----|------|------|------|-------|-----|------|----------|---------|-------|----------|------|------|------|
| 基线（无 A 无 B） | 1.5s | — | — | 2 | **1** | **2** | **2** | 1 | 1 | 1 | success/success | 0 | 3.9s |
| 仅长租约 | 3s | — | — | 1 | 0 | 1 | 1 | 0 | 0 | 1 | success/success | 0 | 3.8s |
| 仅 A 预算 | 1.5s | 120ms | — | 1 | 0 | 1 | 1 | 0 | 0 | 1 | pending/transient | 0 | 1.6s |
| A 预算 3s **超过租约，被运行面封顶** | 1.5s | 3s | — | 1 | 0 | 1 | 1 | 0 | 0 | 1 | pending/transient | 0 | 1.6s |
| 仅 B 冷却 | 1.5s | — | 5s | 1 | 0 | 1 | 1 | 1 | 1 | 0 | pending/（空） | **2** | 2.3s |
| A+B | 1.5s | 120ms | 5s | 1 | 0 | 1 | 1 | 0 | 0 | 1 | pending/transient | 0 | 1.6s |
| 仅 A，提供方吞取消 | 1.5s | 120ms | — | 2 | **1** | **2** | **2** | 1 | 1 | 1 | success/success | 0 | 3.9s |

> 租约必须大于 `attemptLeaseReserve`（1s），否则认领后直接判 `wait_lease_spent`；
> 这里用 1.5s/3s，仍远小于生产 120s。表中数字来自 2026-11-26 真库跑测；
> 第 4 格已从旧报告的“超过租约 ⇒ 重复”变为“运行面封顶 ⇒ 不重复”。

> 矩阵里第 1/5 格的 `budget=0` 是正控支点：`0` 不封顶，才有 `dup=1` 的基线；
> 生产装配不会传 0（配置 0 派生 0.8×租约），所以这两格只用于实验。
> 第 4 格（预算 3s > 租约 1.5s）也是测试直注入，生产被启动门禁止。

读法（四条，缺一条就会误判改动价值）：

1. **只有 A 能在"持有者还活着"的前提下收住重复**：预算 120ms < 租约 1.5s ⇒ 行从不进入过期态
   （`reclaim=0`），提交在 `hold` 到期之前就被自己切断并结算为 `transient`，第二个执行体认领 0 行。
2. **B 单独用会把并发重复变成延迟重复**：第一幕 `reclaim=1`、`fence=1`、`attempts=0`，行停在
   `pending`；第二幕等冷却结束再跑，同一行被重新认领、**再提交一次**（`late=2`），
   这次落到 `success` / `attempts=1`。第一次的成功证据已经丢了——这正是 §1 的后果 (b)。
   注意 `late=2` 是"真实提交两次"，不是"Sign 被调用两次却没做成"。
3. **A 的边界是提供方是否尊重 `ctx`**：最后一格 `dup=1`。预算只能通过 `ctx` 切断，
   吞掉取消的客户端切不断，此时唯一的杠杆是**让持有者在租约到期之前结算完**
   （租约显著大于提供方最坏耗时）。第 2 格只证明这条防线的机制本身——
   `hold=3.8s` 其实已经超过 3s 租约，但第二执行体是在 1.5s（租约内）开跑的，
   所以它认领 0 行、`dup=0`；"租约过期必然被回收"由第 1 格与第 5 格证明。
   一句话：**租约不是超时控制，它只是"到期就允许别人接盘"**。
4. **A+B 是唯一"零重复 + 零回收"的组合**，但它对吞取消的提供方依然等于 B（第 7 格没有 B 也一样 `dup=1`；
   若把 B 加进第 7 格，得到的是"延迟重复"而不是零重复）。零重复的充分条件始终是 A 生效。

## 4. 生产参数建议

| 键 | 建议值 | 依据 |
|----|--------|------|
| `YIBAN_ATTEMPT_BUDGET_SEC` | `0`（派生 0.8×租约） | 派生值恒小于租约；显式值另由启动门要求 < 租约；运行面再按剩余租约封顶；矩阵第 3 格 |
| `YIBAN_TASK_LEASE_SEC` | 保持 `120`，**且必须显著大于提供方最坏耗时** | 第 2 格：租约之内无人能领；第 1/5 格：一旦过期必然被回收。这是不依赖任何客户端行为的唯一防线 |
| `YIBAN_RECLAIM_GRACE_SEC` | `60`（缺省） | 独立保险：把并发重复降级成串行重复、挡住回收抖动/热循环；不是止损 |
| 预算 ≥ 租约 | **禁止**（启动即失败） | 启动门直接拒绝；若测试/嵌入式直接注入，运行面仍按租约余量封顶，矩阵第 4 格 `dup=0` |

"A 生效"的准确条件是：**预算 > 0，且尝试开始时租约余量大于结算保留（1s）**；余量不足时走
`wait_lease_spent` 等待分支，不产生平台请求，也不消耗 attempts。
实际截止 = `min(配置预算, lease_expires_at - attemptStart - 结算余量)`，
所以 `claim → attemptStart` 的前序耗时不用人工估算：它直接从余量里扣。
默认派生 0.8× 是给结算与抖动留 20% 余量。若前序已把租约吃到不足结算保留，
`effectiveAttemptBudget` 返回 `leaseSpent=true`，本次尝试不发出平台请求，
以 `wait_lease_spent` 退回队列且不消耗 attempts。
显式调大 `YIBAN_ATTEMPT_BUDGET_SEC` 也不会突破租约终点。

### 上线注意（默认开启的行为变更）

1. `YIBAN_ATTEMPT_BUDGET_SEC` 缺省 0 **不等于关闭**：装配层派生 `0.8 × 120s = 96s`。
   影响面是"单次尝试耗时落在 96–120s 之间"的账号：过去 `success`，上线后第一次变 `transient`、
   退避重试，三次后 `failed`。线上表现会像"某批账号突然大面积签到失败"，而配置里看不出任何变化。
2. 唯一的现场信号是日志 reason `attempt_budget_exceeded`（`worker.go:72`）；发布时建议同时看
   `task.attempts` 与这个 reason 的出现量。想回到旧行为只能显式设一个接近租约的值
   （仍受"预算 < 租约"的门约束），或临时调大租约。
3. 发布前建议确认目标学校/平台的 P99 单次链耗时；若 P99 已接近 96s，先调大租约再发布。
4. 生产装配下不存在"关闭预算"的配置：0 走派生，显式值小于租约，运行面再按租约余量封顶。
   只有直接调用 `appworker.Deps`（测试/嵌入式）时端口层的 0 才表示不限尝试时长；
   此时租约余量护栏仍在，租约耗尽同样走 `wait_lease_spent`。

未实现的一条后续选项（记在这里，避免下次重新推导）：
**回收打标 + 迟到结算救援**——回收时给行打一个可识别的标（如 `result='reclaimed'`），
并允许"序号仍匹配、行仍是 `pending`"的持有者写终态。这样第 5 格第二幕的"延迟重复"会消失
（第一次成功直接落库，不再重跑）。代价：结算 SQL 多一条分支，且要回答"被回收的行允许谁写"，
故本次只做冷却、不做救援。

## 5. 复现与验证

```bash
# 真库（任意 PG 实例，测试按 schema 隔离）
export YIBAN_TEST_DATABASE_URL='postgres://<user>:<pass>@127.0.0.1:5433/<db>?sslmode=disable'

cd server
go test ./cmd/server/ -run TestLeaseDoubleRunMatrix -v   # 7 格矩阵 + 第二幕
go test ./internal/app/worker/ -run AttemptBudget -v     # 预算的 7 个单测
go test ./internal/adapters/pg/ -run Reclaim -v          # 回收冷却 e2e
./scripts/verify.sh                                      # 全量门（含 gofmt/vet/canonical/logfield/三段真库 e2e）
```

本次跑测（带 DSN，PG 18 容器，独立 schema 隔离）：

| 段 | 结果 | 耗时 |
|----|------|------|
| `gofmt -l` / `go vet ./...` | 通过 | — |
| 全包 `go test -count=1`（剔除三个真库包） | 通过 | — |
| canonical 门（含 `-tests` 两遍） | `gate: ok (44 rules)` ×2 | — |
| logfield 门 | 通过 | — |
| structure / license / generate 门 | `structure: ok (10 scopes, 11 rules)`、`license: ok (37 modules)`、oapi 无漂移 | — |
| pg e2e | **174** 个顶层 PASS（阈值 50） | 53.1s |
| auth e2e（`cmd/server`，含本矩阵 7 格 + 第二幕） | **302** 个顶层 PASS（阈值 45） | 178.3s |
| cli e2e | **158** 个顶层 PASS（阈值 20） | 27.9s |
| container e2e | `TestContainerPhotoDirWritableByNonroot` passed | 86.7s |
| 全程 | `EXIT=0` | **约 7m03s** |

> 本次为最终 HEAD 复跑；auth 段 178s、container 段 87s，均接近空载。
> 数字本身不受影响，只是耗时列不能当基线。

新增用例：`TestLeaseDoubleRunMatrix`（1 个 test / 7 个子用例 + 第二幕，空载 27.5s）、
`AttemptBudget*` 7 例（含发现面截断、末次截断落 `failed`、父 deadline 不误判，0.2s）、
`TestReclaimExpiredAppliesGrace` 1 例。

## 6. 改动清单

7 个提交（6 个功能/修复 + 1 个文档账本），37 个跟踪文件 `+2494 / -93`（`git diff --shortstat 035444a1a..HEAD`）。
其中新增测试文件 4 个共 1239 行（`cmd/server/lease_double_e2e_test.go` 523 行、
`internal/app/worker/attempt_budget_test.go` 403 行、`attempt_budget_internal_test.go` 43 行、
`retry_advice_test.go` 270 行）；既有 `schedule_lease_e2e_test.go` 追加 58 行、
`worker_test.go` 追加 25 行、`worker_waittarget_test.go` 追加 110 行。
下表按 #404 的主面列；#403/#402 的契约、词表与 provider 面见
`attempt-key-claim-cycle.md` 与 `retry-advice-wiring.md`。

| 面 | 文件 | 改动 |
|----|------|------|
| 执行面 | `internal/app/worker/worker.go`、`repo.go` | `AttemptBudget` 端口、`attemptDeadline`/`attemptBudgetExhausted`、两处错误分支的预算判据、`settleOutcome` 改用结算时刻 |
| 存储面 | `internal/adapters/pg/schedule.go` | `WithReclaimGrace`、回收落点 `now+grace`、审计 `grace_sec` |
| 配置面 | `internal/adapters/config/config.go`、`configdata/registry.json`、三份 `.env.example`、`README.md` | 两个新键（`YIBAN_RECLAIM_GRACE_SEC` / `YIBAN_ATTEMPT_BUDGET_SEC`）+ 预算 < 租约的门 |
| 装配面 | `internal/assembly/assembly.go`、`worker_deps_test.go` | 链式注入租约/冷却；`workerAttemptBudget` 派生；镜像判据同步 |
| 词表 | `internal/core/retry/policy.go`、`budget.go`、`signing/conformance/retry_vocab_test.go` | `ClearSession` → `SessionTainted` |
| 审计 | `internal/core/audit/chain.go` | detail 白名单增 `grace_sec`，限幅 0..3600 |
| 门 | `canonical.tsv`、`tools/gate/anchors_test.go` | `env.get` allow 增列本实验测试文件；规则哈希同步 |

### 顺带发现（2 已在 `fix/attempt-key-claim-cycle` 修掉；1 已在本分支修掉）

1. ~~**提供方的 `RetryAdvice` 被整条丢弃。**~~
   **已修**：`settleOutcome` 接收 `result.Retry`；`advisePolicy` 只降 `MaxAttempts`
   （不看 `Retryable`，issue #413），`core/retry.NextDelayWithFloor` 把 `After` 当等待下限。
   详见 [`retry-advice-wiring.md`](retry-advice-wiring.md)。
2. ~~**回收不 bump `attempts`，第二次认领沿用同一个 `attemptID`**（矩阵 `distinct` 恒为 1）。~~
   **已修**：键改为 `"<taskID>#<attempts>.<claim_seq>"`，一次认领周期一个键；
   矩阵两格（基线 / 吞取消）的 `distinct` 随之从 1 变 2；
   A 超租约格已被租约余量封顶，`distinct=1`，不再触发接管。
   根因与取舍见 [`attempt-key-claim-cycle.md`](attempt-key-claim-cycle.md)。
   注：这一项修的是"接管方复用旧键"，挡不住"吞取消 + 短租约"本身——
   那一格仍然 `dup=1`，宿主侧唯一杠杆仍是"租约显著大于提供方最坏耗时"。
