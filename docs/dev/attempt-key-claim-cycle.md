# 幂等键 = 认领周期（issue #403）

> 分支 `fix/attempt-key-claim-cycle`，栈在 `fix/lease-deadline-guard`（PR #404）之上。
> 关联：[#403](https://github.com/frostpolaris1/yiban-auto-sign/issues/403)、`lease-deadline-guard.md`。

## 0. 结论

1. 提交幂等键从 `"<taskID>#<attempts>"` 改为 `"<taskID>#<attempts>.<claim_seq>"`：
   **一次认领周期一个键**，回收后的接管方拿到的是新键，不会被旧执行的结果缓存折叠。
2. 选 `claim_seq` 而不是给回收 bump `attempts`：`claim_seq` 本来就是认领栅栏，
   每次认领递增、且已有 `(task.id, claim_seq)` 语义；回收刻意不动 `attempts`（被回收不是一次结论尝试）。
3. 等待型重扫不再额外拼重扫计数：重扫必走 `RequeueWait` → 重新认领 → `claim_seq` 前进，键自动换新。

## 1. 问题：回收后沿用旧键

`attemptID` 是提交面唯一的幂等键，提供方按它缓存结果：

| 位置 | 事实 |
|------|------|
| `server/internal/signing/providers/yiban/provider.go:159-175` | `key := req.Account.ID + "\x00" + req.AttemptID`；同键加锁串行、结果入缓存 |
| `server/internal/signing/providers/yiban/cache.go` | `resultCache` 无 TTL，容量 1024，满则任意淘汰 |
| `server/internal/adapters/pg/schedule.go:679` `ReclaimExpired` | 只把行放回 `pending`，**不动 `attempts`、不动 `claim_seq`** |
| `server/internal/app/worker/worker.go:750`（改动前） | 键 = `task.ID + "#" + attempts` |

后果在矩阵里是可观测的：`lease-deadline-guard.md` §3 三格有 `reclaim=1`、`signs=2`，
但 `distinct attemptID = 1`。回收把行交给第二个执行体，第二个执行体却拿着和第一个
**完全相同的键**去提交——真实提供方那里这有两种坏结局：

- 第一个调用还在同键锁里：接管方被挡在锁上等旧执行，`Step` 的并发口径失效。
- 第一个调用已写缓存：接管方直接读回旧结论（含 `transient`），**根本不会发请求**，
  "重新认领"变成"重放旧结果"。

这不是文档问题，而是：**认领栅栏（`claim_seq`）已经区分了两次执行，幂等键却没有。**

## 2. 决策

| 方案 | 评价 |
|------|------|
| 回收时 `attempts = attempts + 1` | 会把"被回收"记成一次尝试，污染 `BudgetFor` 的 `MaxAttempts` 预算（`attempts=3` 的 transient 行会在第一次回收后直接 `failed`）；且 `RequeueWait` 已经确立"等待/回收不计尝试"的口径 |
| 新增 `exec_epoch` 列 | 与 `claim_seq` 语义完全重叠（每次认领 +1、用于栅栏），多一列就多一处迁移/回填/对齐 |
| **键拼已有 `claim_seq`** | 零迁移；`ClaimDue` 里 `claim_seq = claim_seq + 1` 已经保证每认领周期唯一；语义直白："一次认领周期 = 一次执行 = 一个键" |

键形状：`"<taskID>#<attempts>.<claim_seq>"`。两段各有职责：

- `attempts` 保留"业务重试"维度：`RequeueRetry` bump `attempts`，同一次认领内的业务重试仍是新键。
- `claim_seq` 提供"执行周期"维度：回收/等待型重扫都会重新认领，键随之换新。

同一租约内重复调用仍是同一个键——这正是幂等缓存要折叠的"同一尝试双发"
（`Step` 的 `errgroup` 与停机重入都可能造成）。

## 3. 改动清单

| 文件 | 改动 |
|------|------|
| `server/internal/app/worker/worker.go:750` | 键改 `fmt.Sprintf("%s#%d.%d", task.ID, task.Attempts, task.ClaimSeq)`；删掉 `waitTargetRescanCount` 分支；注释重写（为什么 attempts、为什么 claim_seq、为什么重扫不用再加后缀） |
| `server/internal/signing/contract/contract.go:157` | `SignRequest.AttemptID` 字段注释：一次认领周期的幂等键，同租约同值、回收后重跑换新值 |

等待型重扫的语义没有丢：`RequeueWait` 会把行放回 `pending`，下一次 `ClaimDue`
必然 bump `claim_seq`；`wait_target:N` 仍用于重扫次数上限与退避计算
（`server/internal/app/worker/wait_target.go:44` 的 `waitTargetRescanCount` 仍被使用），
只是不再参与键的构造。

## 4. 验证

### 4.1 单测（`server/internal/app/worker`）

| 用例 | 钉住的行为 |
|------|-----------|
| `TestRetryUsesFreshAttemptID` | 两次业务重试键 = `[t1#0.1 t1#1.2]`（attempts 与 claim_seq 都前进） |
| `TestAttemptKeyAdvancesPerClaimCycle` | 等待型重扫：`attempts` 保持 0，键从 `t1#0.4` 的下一轮变 `t1#0.6`——换键完全来自 `claim_seq` |
| `TestWaitTargetRescanUsesFreshAttemptID` | 重扫首次提交键 = `t1#0.1`，与等待前扫描区分 |
| `TestPhotoInjected` | 选图用的 attempt 键与提交键同源（`t1#0.1`） |

### 4.2 真库矩阵（`server/cmd/server/lease_double_e2e_test.go`）

`wantDistinct` 由 1 改为 2 的只有两格——**有回收、且第二次认领又提交的格**：

| 格 | 租约 | 预算 | 冷却 | signs | distinct（旧→新） | reclaim |
|----|------|------|------|-------|-------------------|---------|
| 基线 | 1.5s | — | — | 2 | 1 → **2** | 1 |
| 仅 A，提供方吞取消 | 1.5s | 120ms | — | 2 | 1 → **2** | 1 |

旧矩阵的“A 预算 1.2s 超过租约”格在运行面按租约余量封顶后不再回收，
`distinct=1`；它由 `lease-deadline-guard.md` 的租约封顶一节覆盖。
其余五格 `distinct=1`（没有第二次认领，或回收后未再提交）。
整条矩阵 `go test ./cmd/server -run TestLeaseDoubleRunMatrix` 7/7 PASS，空载约 27s。

### 4.3 门

`cd server && ./scripts/verify.sh` 全绿（含 canonical/logfield/gofmt/vet/全包测试/真库 e2e/CLI e2e）。

## 5. 边界与未做

- **本改动不减少"吞取消 + 短租约"的重复提交**：那一格仍 `dup=1`。它把"第二次执行会不会
  读回旧结论"从不保证变成保证；"要不要让第二次执行发生"仍由租约长度与
  `YIBAN_ATTEMPT_BUDGET_SEC` 决定（见 `lease-deadline-guard.md` §4）。
- **缓存基数不升**：`attempts` 已经让每次业务重试换键；`claim_seq` 只把"回收后重跑"从
  复用旧键改成新键，最坏情况是每次回收多一个缓存项，与"每次重试一个缓存项"同量级。
  真要把缓存做成可回收，那是 `providers/yiban/cache.go` 的事（当前 1024 项 + 任意淘汰），
  不在本工单。
- **契约兼容**：`AttemptID` 仍是不透明字符串，提供方不得解析其形状；
  `server/internal/signing/contract/contract.go` 的字段注释已写明"同租约同值、回收后换新"，
  conformance 不需要改。
