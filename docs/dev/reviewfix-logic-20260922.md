# 生产问题 ①-⑤ 的程序逻辑说明（供核实，2026-09-22）

> 目的：先讲清**现状是怎么设计的、代码在哪里**，再列我的建议改法**与设计意图的一致/偏离**。
> 全部结论附 `文件:符号` 证据；标「推断」的是我从代码反推、无文档记载的判断。
> 本文只描述逻辑，不含任何改动。

## 1. 一轮（round）的完整生命周期

```
宿主 cron 06:31 / 07:12 ─┐
容器 scheduler.py ───────┼→ run.sh（加载 .env、导出环境变量）
手动 /api/signin ────────┘        └→ scripts/signin.py（薄壳）→ yiban/cli.py sign → yiban/engine/runner.py::main(argv)
```

`runner.main` 内的顺序（`yiban/engine/runner.py`）：

| 步 | 位置 | 做什么 |
|---|---|---|
| 1 | `:135` | 清空上轮的进程级错误摘要 |
| 2 | `:213-214` | 读 `YIBAN_EXECUTOR_ID` 判**自己是不是子执行体** |
| 3 | `:218-233` | **派发判定**（见 §3）：不是子执行体 + 清单有 >1 个 worker 行 → 派发监督进程后 `return` |
| 4 | `:225-231` | 周末门 / 全局暂停门（`_day_off_skip`），对 `--only`/`--check-config`/`--probe` 豁免 |
| 5 | `:246` | **`load_accounts()` 取账号集合**（见 §2） |
| 6 | `:253-...` | `--second-run-check` 补签判定（见 §5） |
| 7 | `:358` | `schedule.build_schedule` 建调度计划（schedule 模式） |
| 8 | `:434` | `round.run_queue_retry(accounts, ...)` 真正执行 |
| 9 | 收尾 | 汇总 → 写 `sign-daily-<date>.json` → 写 `sched-run-<date>.json` → 退出码 |

## 2. 账号集合：确定时点与过滤链

**关键事实：账号集合在"进程启动那一瞬间"固定，轮内不再重载。**
`yiban/engine/runner.py:246` 调 `load_accounts()` 得到内存列表，其后 `build_schedule`(:358) 与 `run_queue_retry`(:434) 全程复用这一份。

过滤条件分布（三个不同层次，这是问题①的根源）：

| 条件 | 在哪过滤 | 后果 |
|---|---|---|
| `deleted` | `yiban/store/accounts.py::signs_in` / `engine/accounts.py::_load_accounts_from_file` | 不进链 |
| `status ∈ {pending, rejected}` | 同上 | 不进链 |
| **`user_paused=1`** | **只在 `engine/schedule.py::build_schedule`(:356) 把它移出计划**；`runner.py:372` 容量预检也不算它 | **仍在 `accounts` 列表里** → `runner.py:434` 把它交给 `run_queue_retry` |

而 `round.py:294-296`（schedule 分支）对**不在计划里**的账号执行 `_push(_acc, _now0)`——**立即入堆**。`--only`/兜底分支更直接：`round.py:437` `queue = list(accounts)`，同样含暂停号。

跳过点（**在领取之前**）：
- schedule 分支：`round.py:311-316`
- `--only`/兜底分支：`round.py:445-450`
- 领取调用在 `round.py:347` / `:480` —— **所以暂停账号不产生 claim、不占租约**。

**"跳过两次"的成因**：每个执行体各自建堆、各自弹到它、各自写一遍状态；两个 worker 就有两行（生产 06:31:04 与 06:31:14）。`user_cancelled` **不在** `round.py:75 _CLAIM_DONE_STATUSES` 里 → 补签轮与兜底每轮都会再来一次，**永不收敛**。

## 3. 多执行体派发逻辑（问题④的核心）

`runner.py:213-233` 的判定树：

```
YIBAN_EXECUTOR_ID 已设？ ─是→ 我是子执行体，跳过派发，直接跑
        │否
        ↓
slots = egress.launch_slots()   # 清单里 type=worker 的槽位
        │
   清单缺失 → 旧口径：args.workers > 1 才派发
   清单在场 → len(slots) > 1 才派发
        │
        ↓ 派发
workers.run_worker_supervisor(n, argv, slots=...)   # 为每个槽位起一个子进程
```

**关键：这个判定不区分 `--only`。** 所以一次"手动签一个号"在清单有 >1 个 worker 行时，会被展开成**整批执行体**，每个子进程都带同一个 `--only <号码>`，注入不同身份：

- 身份串 `egress.worker_owner(slot)` = `worker-{槽位}@{主机名}`（`api-executors.md:13` 记载三种形态：`worker-{序号}@` / `fallback@` / `single@`）
- 各自持独立锁名 `signin-run.lock.w{slot}`
- 谁先在 `claims.try_claim`（唯一键 `phone+day`，`yiban/store/claims.py`）拿到租约谁签；其余进 `delegated`（`round.py:480-484`）
- 汇总：`round.py` 出 "⇄ N 由其他执行体负责" → 退出码 **2** → 监督进程 `workers.py:173-181` "取最严重"透出 2

**生产实证**：09-22 07:30 手动签 6306 → `worker-0@VM-0-6-ubuntu` 领到并签成（汇总 ✅ 1 成功），slot2 落败退出码 2；网页据此写 `⚠️ 手动签到未完成: 子进程自行退出（…），本轮未实际签到`（`web/services/manual_sign.py:76-79` + `signin_api.py::_reap_signin` 按退出码留痕）——**成功被报成未完成**。

**兜底不在这条链里**：兜底走 `workers.run_fallback_worker`，它**自己每轮 `load_accounts()` 重载**（`workers.py:257`），生产上没运行（见 §4）。

## 4. 兜底（fallback）的设计位置

**设计文档口径**：
- `docs/dev/cli.md:82`：`python3 scripts/signin.py --fallback` —— **"兜底常驻：窗口内反复接手未了结账号"**
- `docs/dev/api-executors.md:106`：`YIBAN_FALLBACK_ENABLE` 是"**.env 里声明的开关**"，且"**声明与在跑是两件事**"，页面要按后端算好的四态显示（`fallback.status`）

**谁拉起它**：
- 容器形态：`docker/scheduler.py::_tick_fallback` 消费该开关
- 宿主形态：`run.sh` **不拉起**它；仓库里有 `scripts/yiban-fallback.sh` 可供 cron/常驻使用
- 生产现状：`.env` 里 `YIBAN_FALLBACK_ENABLE=` 为空（=关）、无兜底进程、日志无"兜底执行体"字样

→ **结论：兜底是"设计里已有、但宿主上没启用"的机制**，而不是"设计里没有的新东西"。

## 5. 补签轮（07:12）的判据

`state_io.need_second_run`（`state_io.py:357`）= `full_run_done_today or has_undone_accounts_today`；其中 `has_undone_accounts_today`（`:327-354`）**只遍历已有记录**。新审核通过的账号没有任何记录 → 不产生"未了结"信号 → 判"无需补跑"（生产 09-22 07:28 实证）。
`_second_run_drop_done`(:148) 只做减法（把已了结的移出）。

## 6. 手动签到链（含冷却与展示）

```
account-ops.js → POST /api/signin
  → web/routes/signin_api.py::_launch_signin_proc (:88-115)
      Popen([python, scripts/signin.py, "--only", phone])   ← 不注入 YIBAN_EXECUTOR_ID
  → 因缺 EXECUTOR_ID 被当监督进程 → 展开整批（§3）
```

**冷却**（`signin_api.py`）：
- 键 `YIBAN_BATCH_SIGN_COOLDOWN_SEC`，**默认 1800s**（`:161` 单条 / `:260` 批量）
- 计数器 `app.extensions["yiban_signin_state"]["last_batch_ts"]`（`:313-314`）——**全局、单条与批量共用、web 重启归零**
- 另有按账号防抖 `SIGN_MIN_INTERVAL = 30s`（`web/app.py:691`，`state["last_trigger"][phone]`）
- UI 只在被拒后 toast（`account-ops.js:122-135`），无倒计时
- **设计意图无文档记载**（`docs/` 全仓无该键的设计说明，「推断」它是"防误点/防审计噪音"性质）

**展示**：账号页「上次实领」列 `last_executor` 来自领取池 owner（`accounts_api.py:71,94` → `executor_env._last_executors`），**与自动轮次同源**，无法区分是否手动触发。

## 7. 日志产生点（对应问题⑤）

7 天 1,773 行、ERROR 0、INFO 95%；`yiban` + `yiban.client` 占 56.7%；归一后只有 88-121 种句式（冗余 89-93%）。
每账号稳定 5 行（`登录成功` / `生成定位` / `签到成功，剩余 1 个任务` / `✅ 签到成功` / `会话缓存作废`），3 天 611 行 = 73.6%。
→ **乱在"每个账号的每条内部状态都独立成行 + 同一件事两个 logger 各写一遍"**，不是格式问题。

## 8. 建议改法 × 设计意图（逐条）

| 项 | 现状设计 | 建议改法 | 与设计意图的关系 |
|---|---|---|---|
| ① 暂停账号 | 计划层剔除 + 执行层跳过并留痕 | 入口剔除 + **统一补写一次** `user_cancelled` + 纳入"当日已了结" | **向意图收敛**：留痕语义不变，只是把"每个执行体各跳一次"改成"一次"。需定：日历是否留痕（你选留）、`--only` 是否照签（你选照签） |
| ② 新账号入轮 | 集合=启动快照；兜底每轮重载但宿主未启用 | 你选"靠下一轮带上" → 落地机制需定（见 §9） | **`--fallback` 的原始设计就是"窗口内反复接手未了结账号"**（`cli.md:82`）——启用它=用回设计里的机制；**但要改变宿主"每天两轮"的节奏**，这是需要你核实的地方 |
| ③ 冷却 | 全局 1800s，单条/批量共用 | 同号 60s + 全局速率上限 | **改变设计**（原按"全局动作"限流，改为"按账号"限流）；意图无文档，需你定义：它防的是误点还是被盗会话 |
| ④ 手动签到 | `--only` 也走整批派发；退出码 2 语义含"别人签了" | a) `--only` 不派发；b) 退出码/文案区分"别人签了" | **向意图收敛**：`cli.md:79` 写的是"`--only` **只签指定账号**"，为单个号拉起 N 个执行体与该口径不符；退出码 2 是 run.sh/容器共用契约，需同步 `cli.md §3` 与测试 |
| ⑤ 日志 | 每账号 5 行扇出 | 三步降噪（详见上一份实证） | 纯 INFO 层收敛，不改 WARNING/ERROR 与日志页解析契约 |

## 9. 需要你核实的三个点

1. **②的落点**：把兜底在宿主上跑起来（`scripts/yiban-fallback.sh`，窗口内常驻、窗口关闭自退、每轮重载账号）——**是否接受宿主在窗口内由"两轮"变成"兜底常驻反复扫描"**？（设计文档支持这么做，但节奏变了；另一种是保持两轮、只让补签轮能看见新号，代价是 07:12 后过审的号仍漏。）
2. **③的定位**：全局 1800s 冷却**原本防的是什么**？（若防"被盗会话刷真实登录"，那按账号放开就需要保留一个全局速率上限——你已选该方案，请确认上限值 10 次/600s 是否合适。）
3. **④的批量语义**：`--only` 不派发之后，**批量手动签到 N 个号会退化为单执行体串行**（与"被一个执行体整包领走"等价）。是否接受？（若希望批量仍并行，则需给手动链一个固定的执行体身份，属新增设计。）
