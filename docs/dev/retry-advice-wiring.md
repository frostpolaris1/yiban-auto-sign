# 接线提供方的重试建议（issue #402）

> 分支 `fix/retry-advice-wiring`，base `origin/develop` `035444a1a`。
> 关联：[#402](https://github.com/frostpolaris1/yiban-auto-sign/issues/402)。
> 与 PR #404 有已知的小冲突：`core/retry.Policy` 的 `ClearSession` 在 #404 里改名为
> `SessionTainted`；本 PR 不碰该字段，合并时按 #404 的名字 rebase 即可。

## 0. 结论

1. `contract.RetryAdvice` 从"生产 + 校验但无人消费"改成真正的结算输入：
   `After` 是**等待下限**（`NextDelayWithFloor`），`MaxAttempts` 是**上限建议**
   （只在更小且 `Retryable` 时压低宿主预算）。
2. **不做 `!Retryable` 一票否决**：零值建议（老提供方不填 `Retry`）与
   "明确不重试"无法区分，否决会把它们的正常重试全掐掉。提供方要表达不重试，
   正确写法是 `Retryable=true` 且 `MaxAttempts=1`（只填 1 会被当作没有建议）。
   这条写法目前**只对 `transient` 生效**：conformance 的 `checkSignRetry` 只允许
   `transient`/`challenge` 标 `Retryable=true`，而 `challenge` 在宿主侧走 `skipped`；
   `pending_upstream`/`unknown` 既不能被 conformance 接受 `Retryable=true`，
   `Retryable=false` 的建议又会被宿主忽略——已另开 issue #413。
3. 易班的 `After` 保持 0 并写明理由：平台无文档化固定冷却，宿主 60s 基础间隔
   已覆盖重发节奏；凭空编下限只会白等。

## 1. 问题：建议被整条丢弃

| 位置 | 事实 |
|------|------|
| `server/internal/signing/contract/contract.go:245` `RetryAdvice` | `{Retryable, After, MaxAttempts, ClearSession}`，由 `Result.Retry` 携带 |
| `server/internal/signing/providers/yiban/provider.go:389` `retryFor` | transient → `{Retryable:true, MaxAttempts:3}`；`After` 从未赋非零值 |
| `server/internal/signing/conformance/suite.go:488-556` | 校验提供方填的建议形状 |
| `server/internal/app/worker/worker.go`（改动前） | 结算只看 `core/retry.BudgetFor`，从不读 `result.Retry` |

`grep '\.Retry\b' server/internal --include=*.go` 在改动前除 conformance 外零命中：
适配器能说"请等 5 分钟再试"或"这个错重试一次就够"，调度面完全看不见。
这不是文档瑕疵——它是**契约承诺与执行面的静默分歧**：conformance 保证字段被填对，
但填对了也没有任何后果，后续提供方会以为自己的建议生效。

## 2. 决策

| 方案 | 评价 |
|------|------|
| 删掉字段 | 已有多提供方实现与 conformance 校验；删掉会把"平台侧冷却"这个真实信息通道一并关死 |
| 只打分歧日志 | 可观测但仍不执行；"知道建议却不听"比"不知道"更难解释 |
| **接线（本 PR）** | `After` 作下限、`MaxAttempts` 作上限建议；零值保持宿主缺省 |

具体规则：

- `MaxAttempts`：仅当 `advice.Retryable && advice.MaxAttempts >= 1 && advice.MaxAttempts < policy.MaxAttempts`
  才压低。抬高永远不允许（宿主预算管上限）。
- `After`：`NextDelayWithFloor` 里两处生效——先抬名义间隔（`base = max(base, floor)`），
  让既有"窗口尾段放不下就放弃"的判据同时覆盖下限；抖动后再夹一次，防止
  `[0.5, 1.5]` 抖动把 `floor=5min` 压成 3min。
- `!Retryable`：不作为否决条件（见 §0 第 2 条）。
- `ClearSession`：本 PR 不动。宿主目前**完全没有消费**该字段（`app/worker` 不读它，易班清的是
  自己的会话缓存），所以契约注释已改成"尚未消费、仅为记录"；真要做"主动清会话"，得先在会话
  仓储上定义清除动作与审计（与 #404 把 `Policy.ClearSession` 改名为 `SessionTainted` 是同一件事的两半）。

## 3. 改动清单

| 文件 | 改动 |
|------|------|
| `server/internal/core/retry/budget.go:95` | 新增 `NextDelayWithFloor(p, attempt, w, now, floor)`；`NextDelay` 委托 `nextDelay(..., 0)` |
| `server/internal/app/worker/worker.go:425` | `settleOutcome` 增 `advice contract.RetryAdvice` 参数；成功路径传 `result.Retry`，错误路径传零值 |
| `server/internal/app/worker/worker.go:469` | 新增 `advisePolicy`（`MaxAttempts` 只压不抬） |
| `server/internal/app/worker/worker.go:448` | 退避落点改 `NextDelayWithFloor(..., advice.After)` |
| `server/internal/signing/contract/contract.go:245` | `RetryAdvice` 注释写明宿主消费口径与"零值 = 没有建议" |
| `server/internal/signing/providers/yiban/provider.go:389` | `retryFor` 注释写明 `After` 为何留 0 |

## 4. 验证

### 4.1 `core/retry`（`budget_test.go`，4 例）

| 用例 | 钉住的行为 |
|------|-----------|
| `TestNextDelayWithFloorRaisesBase` | floor 5min > base 60s ⇒ 落点 `[5m, 7m30s]`（抖动的下界被夹住） |
| `TestNextDelayWithFloorGivesUpBeyondTail` | 窗口只剩 10min（cap 3min）< floor 5min ⇒ 放弃 |
| `TestNextDelayWithFloorZeroEqualsNextDelay` | floor=0 等价旧曲线 |
| `TestNextDelayWithFloorDoesNotShrink` | floor < base 时不改变曲线 |

### 4.2 `app/worker`（`retry_advice_test.go`，5 例）

| 用例 | 钉住的行为 |
|------|-----------|
| `TestRetryAdviceRaisesBackoffFloor` | 建议 `After=4min` ⇒ 落点 `[4m,6m]`（默认 60s 被抬） |
| `TestRetryAdviceCapsAttempts` | 建议 `MaxAttempts=1`（宿主 3）⇒ 第一次失败即 failed、不重排 |
| `TestRetryAdviceDoesNotRaiseAttempts` | 建议 `MaxAttempts=9` ⇒ 仍按宿主 60s 曲线重排 |
| `TestRetryAdviceZeroKeepsHostPolicy` | 零值建议 ⇒ 与改动前完全一致 |
| `TestRetryAdviceFloorBeyondTailGivesUp` | 下限超出窗口尾段份额 ⇒ failed，不越窗重排 |

### 4.3 门

`cd server && ./scripts/verify.sh` 全绿（含 canonical/logfield/gofmt/vet/全包测试/真库 e2e/CLI e2e）。

## 5. 边界与未做

- **不改 `ClearSession` 的处置**：见 §2 最后一条。真要做"清会话"，需要先在
  会话仓储上定义清除动作与审计，再让 `Policy.SessionTainted`/`RetryAdvice.ClearSession`
  汇到同一个执行面；那是独立工单。
- **易班不填 `After`**：没有实测到的固定冷却就不写死数字。若将来响应里带回
  cool-down，填进 `RetryAdvice.After` 即可自动生效（本 PR 的接线是通用面）。
- **下限只作用于业务退避**：等待型重扫（`wait_target`）走自己的 base 5min 曲线，
  与 `RetryAdvice` 无关；这是既有口径，不在本工单内合并。
- **"限次建议"的合法域不全**：见 §0 第 2 条与 issue #413。要让
  `pending_upstream`/`unknown` 也能限次，得先决定是放宽 conformance 还是给
  `RetryAdvice` 加一个"显式建议"标志，不在本 PR 内改判定表。
