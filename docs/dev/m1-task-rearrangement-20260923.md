# M1 任务重排（直接派发模式，2026-09-23）

> **是什么**：M1（后端容量升级）在原工作流被用户主动暂停后的**任务序重排**：已完成部分的交接、剩余五个任务的划分与依赖、门禁与派发纪律。
> **状态**：生效（2026-09-23）
> **范围**：只覆盖 M1 剩余任务（任务 6~10）；不含 M2 审查流、M3 修复流、M4 前端流，也不含 S1 安全瘦身批（排在 M1 合并之后）。
> **读者**：本次派发的实施者与评审者必读；M2 审查者选读。
> **基线**：`feature/backend-scheduler-v3 @ 32aab79`（worktree `D:/code/yiban-wt-schedv3`）

**关键词**：M1、任务重排、台账、sign_ledger、自选片映射、executor_v3、capacity_of、窃取协议、直接派发、WSL 门禁

## 速览

- 任务 1~5 已落地（11 提交），**任务 6/7 从未开工**；原 7 任务制改为 **10 任务制**，新增的 6/7/8 来自本日两份新设计（台账双版本、自选片映射迁移）。
- 执行序 **6 → 7 → 8 → 9 → 10**，串行（同一 worktree，避免提交竞态）；每任务 TDD → WSL 门禁 → 独立评审 → 修复（≤3 轮）。
- 直接派发模式（不用工作流）：子代理模型**不可逐运行指定**（Agent 工具无 model 参数），统一继承会话模型。
- 任务 6 的验收核心是**零行为变化**：四谓词必须与现行 `UNDONE_STATUSES` / `_CLAIM_DONE_STATUSES` / `_has_conclusion` 逐格等价。

## 目录

1. 已完成部分与遗留
2. 重排依据
3. 新任务序与依赖
4. 各任务范围与验收门
5. 派发纪律与门禁命令
6. 与后续批次的接口

---

## 1. 已完成部分与遗留

`b6457e6..32aab79` 共 11 提交，任务 1~5 全部落地：

| 任务 | 提交 | 产物 |
|------|------|------|
| T1 迁移 v18 + queue_store | `1b9de9d` `f5caa3d` | `sign_tasks`/`executor_heartbeats`/`egress_state` 建表 + v18 平移 + 访问层（其中 `executor_heartbeats` 已于 2026-09-23 按 R-T5 删除） |
| 测试裁剪 | `8f6ea73` `818ff78` | 删 31 合并 16；一次性锁文件并入主题测试 |
| T2 fencing epoch | `a2bc227` | v19 迁移补 `epoch` 列；`try_claim` 异常改 fail-closed |
| T3 HRW + 256 虚分片 | `c1fffc0` `3c80b9f` | `yiban/engine/hrw.py`（纯函数） |
| T4 双粒度 Planner + 容量公式 | `4793ad5` `48c5cf3` | `yiban/engine/planner.py` + `capacity_accounts_v3` |
| T5 出口令牌桶 + AIMD + Λ + gap 门 | `5f6f262` `32aab79` | `yiban/engine/token_bucket.py` + `egress_state` 落库 |

**遗留**：任务 5 的评审未收口（`32aab79` 是评审后的修复提交，第 2 轮意见未见终判）——本次以 T6 之前的**一次独立复核**补上，不重开任务 5。

**已知死物**（本批不清理，登记在案）：`executor_heartbeats` 已按 R-T5 从 v18 删除（职责由文件心跳承担）；`EgressLimiter` 无生产调用点（T9 接线）。

## 2. 重排依据

原工作流的 7 任务制建立在「10K 调度设计」一份文档上；本日新增两份设计改变了剩余任务的内容：

1. **`docs/dev/ledger-dual-version-design-20260923.md`（台账双版本）**——原 T6/T7 的实现必须建立在统一台账之上，否则会把新消费者写到分裂口径上。故把「台账 A 阶 / B 阶」插到执行体之前（原计划的执行序 2,3,4,5,8,9,6,7 同理）。
2. **同文档 §8（自选时间片映射迁移）**——几何函数 `_slot_to_bi` 挂在 v2 模块却被 v3 planner 反向依赖，必须在删 v2 路径之前中立化；且 E9 的 bounds 准绳分叉要一并统一。
3. **核验结论**（§9 现状核验记录 @32aab79）：迁移顶是 **v19** 不是 v18；`capacity_accounts_v3` 零生产调用；`window.py` 是叶子模块（迁移无循环 import 风险）；**v18 平移缺口属实**。

## 3. 新任务序与依赖

```
T6 台账 A 阶 ──┬─→ T7 台账 B 阶 ──┐
（词汇表/门面/  │  （v20/backfill/  │
  消费者切换）  │    ledger_check） │
               │                   ├─→ T10 web 闸门 capacity_of
               └─→ T9 executor_v3 ─┘    + 窃取协议 + scheduler-v3.md
T8 自选片映射迁移 ──→ T9
```

- **T7 ← T6**：backfill 用 `from_status_json`（T6 产物），`ledger_days` 与门面同库。
- **T9 ← T6**：执行体的「已了结」判定必须走 `sign_ledger` 谓词，不得直接读状态串。
- **T9 ← T8**：planner 的几何依赖已中立化（`window.slot_to_bi`），避免与 T9 同改 planner 冲突。
- **T10 ← T7**：`capacity_of` 按当日 `ledger_days.writer` 选公式。
- **T10 ← T9**：`K` 自动公式与窃取协议需要执行体侧真实调用点。

## 4. 各任务范围与验收门

| 任务 | 一句话范围 | 关键验收 |
|------|-----------|---------|
| **T6 台账 A 阶** | `ledger_states`（六态 + 合并格 + 四谓词 + 三映射）+ `sign_ledger` 门面 + 六处消费者切换 + `claims`/`queue_store` 枚举收口 | **逐格等价矩阵**（与 `UNDONE_STATUSES`/`_CLAIM_DONE_STATUSES`/`_has_conclusion` 全等，唯一例外：未知状态串→FAILED）+ I-2 grep 0 命中 |
| **T7 台账 B 阶** | v20 迁移（`ledger_days` + backfill 补 JSON 终态）+ `fold` 抽象 + `scripts/ledger_check.py` | `ledger_check` 三方对账全平 + 重跑幂等 + 旧代码开新库冒烟（I-5） |
| **T8 自选片映射迁移** | `_slot_to_bi` 迁入 `yiban/window.py` 并套 bounds 回退 + web `_pref_slots` 消重 + 注释订正 | I-7（v2/v3 落点 ∈ 所选片）+ I-8（片集合同源）+ E9 回归（裁剪配置下不再全丢） |
| **T9 executor_v3 + 分流** | asyncio M 通道执行体 + `to_thread` 过渡 + 退避 + 心跳 + `YIBAN_SCHEDULER_V3`（默认 0）+ dry_run 影子 | 默认 0 时旧路径**零变化**；dry_run 不写领取行；影子与真实落点可对账 |
| **T10 web 闸门 + 窃取 + 文档** | `capacity_of` 按 writer 选公式（四处调用点）+ K 自动公式 + 分片级窃取协议 + `docs/dev/scheduler-v3.md` | 误拒区间消失；窃取只动 `pending` 且 `epoch+1`；文档 8 节齐 |

## 5. 派发纪律与门禁命令

**纪律（每个子代理提示词必须原样带上）**

- 只在 worktree `D:/code/yiban-wt-schedv3` 改代码；**永不**改 `D:/code/yiban-auto-sign`（主工作区，只读文档）与 `D:/code/yiban-wt-study`（study 线，只读参考）。
- **永不** `git stash pop/apply`；**不 push**（合并由主线负责人做）。
- 注释遵守 `docs/dev/python-script-contract.md`：四问头部（功能/归属/复用/通信，通信须写调用点）、函数注释只写「为什么」、**无任务痕迹**（批次号/日期/工单号/"修了什么"）、跨文件引用用符号名不用行号。
- 脱敏：日志与文档里的手机号一律 `138****0000`。
- `.env` 既有键语义不变；退出码契约 `0/1/2/3/10` 逐字保留。
- 提交信息只写技术要点（中文，conventional 前缀），不写任务号。

**门禁命令（WSL）**

```bash
cd /mnt/d/code/yiban-wt-schedv3 && source ~/.venv-yiban-wsl/bin/activate && \
  ruff check yiban/ tests/ scripts/ --quiet && \
  python -m pytest tests/ -q -n auto --dist loadfile 2>&1 | tail -8
```

定向阶段可先用 `-k` 缩小；**提交前必须全量跑一次**。

> **并行口径（2026-09-23 实测，R-T13）**：`-n auto --dist loadfile`（按**文件**分发、文件内顺序保留）实测 **2724 passed / 4 skipped / 0 failed / 63.45s**，与串行（~400–420s）数字逐项一致，**约 6.5×**。
> **不要**用裸 `-n auto`（默认按单条测试分发）：它会把同一个类拆到不同 worker，叠加进程级 DB 连接单例，**确定性失败** 1 条（`test_web_security_gates::Batch18FixesTest::test_cookie_path_narrowed_when_base_path_env_set` 报 401）。
> **CI 的 `pytest -q -n auto` 同样必须加 `--dist loadfile`**（否则那条会红）。

**已知基线失败**（4 条，按方法名后缀匹配，不算门禁失败）：`test_login_failure_still_reaches_the_notification_layer`、`test_health_report_states_both_channels_even_when_closed`、`test_exhaustion_notice_pops_once_and_sends_one_mail`、`test_notify_capacity_once_uses_app_send_notification`。
> 实测订正（2026-09-23）：这 4 条在本 WSL 环境**不复现**（全量 0 失败，且在纯基线快照 `32aab79` 上单跑也通过）——它们是**环境敏感**用例，保留此列表只为"万一在别处复现时不被当成新增失败"。

- **环境（2026-09-23 实测）**：
  - WSL 里 **`node v22.22.1` 已装**（此前缺失 ⇒ `tests/test_logs_by_date.py`、`tests/test_web_mask_email_parity.py` 长期被 `shutil.which("node")` 判为不可用而 **skip**；装上后实测 **32 passed**）。装法：`sudo apt-get install -y nodejs npm`（Ubuntu 26.04 源，sudo 免密）。
  - **git 必须走 Git Bash，WSL 只跑 pytest/ruff**：worktree 的 `.git` 指向 Windows 路径（`D:/code/yiban-auto-sign/.git/worktrees/yiban-wt-schedv3`），在 WSL 里执行任何 git 命令都会 `fatal: not a git repository`。故子代理的 git 操作与门禁的 pytest 分属两个 shell，提示词里要分开写。

## 6. 与后续批次的接口

- M1 合并进 `develop` 后，M2（逐文件审查 + 注释流）在**新基线**上跑；本批任务简报与评审意见是 M2 的输入。
- **MF-1（生产无隐私遮罩）**：M2 查根因、M3 必修，本批不得裁撤相关代码。
- **S1 安全瘦身**：排在 M1 合并之后、M2 之前（`docs/dev/security-slim-plan-20260923.md`）。
- 推送门（`PROMPT.md` §6.10）：合 `main` 需 ≥3 个有效轮次（跨 ≥2 天）+ 4 项通用门禁；本批不触 `main`。

## 7. 兼容性硬约束（服务器版本）

**生产现状**：v0.4.7，`server-web @ 6d4eafa`，库 `user_version` 低于本分支（本分支顶为 v19）。本批产物必须能与生产版本**读同一个库**。

**机制已核验**（`yiban/store/migrations.py:836-891`）：`version = PRAGMA user_version` 后逐项 `if version >= target_version: continue` ⇒ **旧代码打开 `user_version` 更高的库时整链跳过、不报错、不改库、不降版本**。这就是 I-5「旧版无感」的实现依据。

**由此推出本批的 schema 纪律（每任务验收都要过）**：

1. **只增不改**：新增表 / 新增列一律带 `DEFAULT`（先例：v19 的 `epoch INTEGER NOT NULL DEFAULT 0`）；**不做** 改名、DROP、删列（Phase 5 退役已推迟，见 §8）。
2. **开关缺省关**：`YIBAN_SCHEDULER_V3` 缺省 `0` ⇒ 部署到生产后 v2 路径逐字不变。
3. **旧代码冒烟**（T7 落地为测试）：把库 `user_version` 抬到高于旧代码迁移顶，断言 `_run_migrations` 不执行任何迁移且不抛。
4. **不触生产**：本批不合并 `server-web`、不改 crontab、不重启服务；部署顺序（develop → server-web）由主线负责人在 M1 合并后再定。

## 8. 最小化裁剪（2026-09-23 决策）

对 §4 的任务范围做**最小化裁剪**——只保留「修真实缺陷 / 有红线依据 / 生产版本兼容必需」的项，其余全部推迟到 v3 灰度批（即真正把 `YIBAN_SCHEDULER_V3` 打开的那一批）。**§4 的范围表述以本节为准。**

| # | 决策 | 取值 | 理由 | 可推翻性 |
|---|------|------|------|---------|
| 1 | 交付边界 | 最小闭环 + 保留 backfill | backfill 补的是「同日跨版本接管会重处理已跳过账号」，成本一迁移一脚本 | 若认为 v3 开闸前不会跨版本接管，可并入灰度批 |
| 2 | 跨版本恰好一次 | **不建 `ledger_days` 表**，写者由开关推出 + 运维纪律（切换只在日界） | `YIBAN_SCHEDULER_V3` 是静态开关；红线由 `PK(phone,day)` + epoch fencing + `already` 幂等兜住 | 若要做代码级防呆，加回表 + 两处领取边门 |
| 3 | JSON 空状态口径 | **保持现行**：`""`/缺键 = 无记录，不算未了结、不算有结论 | 现行 `UNDONE_STATUSES` 对空串答否；改了会动补签行为，违反零行为变化 | 若要 fail-safe，需登记为显式行为变更 |
| 4 | v3 日可见性 | **保持 v2 契约**：v3 在 settle 期周期写 `sign-state`，分流时补写 `sched-snapshot` | v2 是逐账号增量写，只在收尾 materialize 会让日历整天空窗 | 若接受空窗，删掉周期写、只留收尾物化 |

**逐任务的裁剪结果**

| 任务 | 保留 | 删除/推迟 | 文件数 |
|------|------|----------|--------|
| T6 | `status.py` 收口三个判定集合（`CLAIM_DONE_STATUSES` / `CONCLUDED_JSON_STATUSES` / tasks 侧两集合）；`claims`/`queue_store`/`round` 改为引用同一对象；`_has_conclusion` 改用共享集合 | 新建 `ledger_states.py`、`sign_ledger` 门面全族、合并格 rank/join、三映射函数、六处消费者切换、I-2 全仓 grep 门、392 格等价矩阵 | 4 改 + 1 测试 |
| T7 | backfill（v20 迁移，只读 `sign-state-*.json`）+ 瘦 `ledger_check`（只查「JSON 终态是否都在 sign_tasks」）+ 旧代码冒烟测试 | `ledger_days` 建表、`ledger_fold.py`/`fold`/`FOLD_MAP`、`materialize`/`to_status_json`、backward/回滚、`_ALLOWED_TABLES` 增补 | 2 改 + 1 新 + 1 测试 |
| T8 | 就地修 E9（`_slot_to_bi` 套 `window.bounds` 回退）+ 三处注释订正（`time_prefs.py:123`、`accounts_data.py:317`、`schedule.py:383`）+ 回归测试 | 几何迁入 `window.py`、planner/schedule import 改写、web `_pref_slots` 消重（I-8） | 3 改 + 1 测试 |
| T9 | `executor_v3` asyncio 核心（M 通道 + `to_thread` + `claim_batch` 32/事务 + token_bucket 接线 + 有界退避）+ `YIBAN_SCHEDULER_V3`（缺省 0）分流 + settle 期周期写 `sign-state` + 补写 `sched-snapshot` | 表心跳写入口与 `heartbeat_loop`、`outcome_buffer`、`shadow:` 影子行与领取边拒领、`ledger_days.writer` 领取门 | ~4 改/新 + 1 测试 |
| T10 | `capacity_of` **按开关**分派（不查库）+ K 自动公式 + 四处调用点 + `docs/dev/scheduler-v3.md` | `dead_owners`/`steal_dead_vshards`（并入灰度批，改按租约失效）、`YIBAN_ACCOUNT_GAP_HARD` 新键 | 4 改 + 1 文档 + 1 测试 |

**推迟项的去向**：全部并入「v3 灰度批」（打开 `YIBAN_SCHEDULER_V3` 的那一批），届时按原设计文档 §2/§3/§8 逐项加上——**每项都是独立可加的**，不需要返工：门面与合并格在 v3 有真实消费者时再加；`ledger_days` 与几何迁移在退役前再加。

---

## 明确缺陷

| 编号 | 缺陷 | 影响 | 处置 |
|------|------|------|------|
| R1 | 直接派发模式下子代理模型不可指定（Agent 工具无 model 参数），与此前「逐运行确认模型」的做法冲突 | 无法对重任务上调推理档 | 已在速览披露；如需分档请改回工作流模式 |
| R2 | 任务 5 的评审未收口（末次修复 `32aab79` 后无终判） | 令牌桶模块的评审结论缺失 | 在 T6 之前补一次独立复核，不重开任务 5 |
| R3 | 任务 6/7 的旧简报（原 executor_v3 / web 闸门）已顺延为 task-9/10-brief.md，若有人按旧编号读会错位 | 简报编号与旧文档不一致 | 旧简报文件头已改标新编号（准备者落实） |

## 待办

| 编号 | 事项 | 处置时点 | 状态 |
|------|------|---------|------|
| R-T1 | 补任务 5 与任务 6 的独立复核（R2） | T7 前 | 已完成（`docs/dev/review-t5-t6-independent-20260923.md`；9 条修复轮见 `cc0bd5b`/`3632b7e`/`6c3167c`） |
| R-T2 | 简报落盘：6/7/8/9/10 已按最小版重写并实施完毕（旧版存 `superseded/`） | 即刻 | 已完成 |
| R-T3 | `_slot_to_bi` docstring 里 `_pref_slots` 的归属写成 `web/app.py`（实为 `web/routes/my.py`） | T8 | 已完成（随 T8 订正） |
| R-T4 | 生产待办：B4（备份哨兵）与 **B3 的可观测性优化**（07:12 那条重复触发与首轮撞锁、日志读起来像「没跑」）。**B3 不是补签机制缺陷**——补签已由 `run.sh` 的进程内第二轮承担（2026-09-10 批次20 方案一，判定收敛在 `state_io.need_second_run`），07:12 cron 是首轮被 timeout 击杀时的安全网（`run.sh:62-78`）；生产 09-19 日志实证该轮会等待到 07:12 并执行 | S1c 批 | 未开始 |
| R-T5 | **schema 简化**：① 从**未发布的 v18** 删掉 `executor_heartbeats` 建表（真死表：3 列零引用，职责已被 `state_io.mark_worker_beat`/`worker_presence` 文件心跳覆盖）；② `egress_state` 建议**保留**（T9/T10 要接线出口限速），但只写不读的 `updated_at` 可去掉；③ `sign_claims` + `sign_tasks` 双表按既定退役走，**过渡期不得引入双写**。**裁决：删**（2026-09-23）；`egress_state` 保留（T9/T10 接线）；双表退役按既定计划、过渡期不得双写 | T7 前裁决 | 已完成（代码 `1dbe126`） |
| R-T6 | `yiban/engine/runner.py:508`（`has_executed`）与 `:553`（日状态符号白名单）仍手写同一组状态字面量；`claims.stats` 的 settled 身体不读共享集合——是否收口到 `yiban.status` | 下一轮 | 未开始 |
| R-T7 | **脱敏是两件套**（`yiban.masking.sanitize_text` 遮凭据、`mask_phone` 打码号码）：异常/兜底路径目前只做前者（与 `state_io` 既有口径一致），若异常消息回显号码则原样落盘；`ledger_check` 的差异行走 `mask_phone`。M2 的遮罩审查（含 MF-1）一并核「所有日志与工具输出路径是否都同时做两件」 | M2 | 未开始 |
| R-T9 | **注释轮增补：测试文件与函数的标签注释**（用户 2026-09-23 提出，目标是让工具按标签快速检索/定位/报告）。**前置**：M1 合并后、测试冻结在合并基线（当前 T8/T9 仍在改测试，先做必冲突）。**分层做法**：① 每个测试文件的模块 docstring 规范成固定几行（`覆盖` / `对应实现` / `关键断言` / `依赖`），标签直接用 `docs/dev/test-inventory.md` 的 13 个组名（可 grep）；② 函数级标签**只补命名不自解释的**（编号式如 `test_08b_*` / `test_15c_*`）；③ 另出一份**生成式索引**（脚本从 docstring + 命名产出 JSON/TSV，带 `--check` 门禁防过期）——手写两千多处必然漂移，索引不会。**给 `python-script-contract.md` 补一条测试专用条款**：测试函数注释允许且应当写「这条在守什么」（测试即规范），生产代码仍只写「为什么」。**硬门不变**：只动注释/docstring（AST 等价）、分批提交、测试断言不得改。**已随交接文档更新**：`docs/dev/handoff-comment-annotator-prompt.md` 已重写为「范围 A 代码注释 + 范围 B 测试标签」两件事的唯一任务书（含固定字段模板、13 组标签词表、两份报告格式、环境提示） | M2 注释流 | 就绪待放行（任务书 `handoff-comment-annotator-prompt.md` 的基线已换为 `b6457e6..9d3f491`、实测 73 个改动文件；分组与派发方式见 `m2-dispatch-plan-20260924.md` §3） |
| R-T10 | **审查流任务书已写**：`docs/dev/handoff-review-stream-prompt.md`。含 **L1** 单文件逐行逐函数（业务逻辑 / 代码简化 / 安全 三维度，含"简化的反判据"）、**L2** 上下游链路聚合（点名 5 条链：签到主链 / 补签链 / 数量与状态一致性 / 配置链 / 写入与恢复链）、**L3** 全功能 E2E 情况矩阵（约 18 格）+ 性能折算（500/1 000/5 000/10 000，**必须展示算式**并与 `40-capacity-estimate.md` 与设计 §11 对账）、**S** CLI 黑箱专项（硬协议：只给测试项目 → 只用 CLI 自己的输出 → **遇 bug 或找不到功能前不许读码** → 触发后只读最小范围 → 记录卡住点；对人/对 agent 两条线分开评）、**只读生产授权**（`ssh yiban` + 白名单 + 禁读口令/密钥/.env 敏感值 + 报告脱敏）与 **MF-1 列为必查项**（用生产日志取证） | M2 审查流 | 已完成 |
| R-T11 | **MF-1 根因已定位 → 修复轮**（根因与证据见 `must-fix-list.md` MF-1）：无兜底脱敏（全仓零 `addFilter`）+ `mask_phone` 仅少数调用点手工调用 ⇒ `round.py` 的 `[{phone}]` 系列与 `session_cache.py:173-174` 直印裸号（生产原始日志实测；`session_cache` 每账号一行，当日 78 行）。**修法**：① 日志输出面挂脱敏（`Formatter` 子类或 handler `Filter`，幂等「11 位号→前3****后4」），挂载点先清点（引擎 CLI / web / 容器各自的日志初始化）；② 点修 `session_cache.py:173-174`；③ 导出/展示层复用同一实现；④ 补**可执行不变量测试**（含裸号的记录 → 输出为遮罩）。**排期**：插在 **T9 之前**（改动小、隐私必修、且与 T9 不冲突）——若用户希望先做 T9 再修，改本行即可 | M3（提前至 T9 前） | 已完成（`2437bfb`；`MaskingFormatter` 挂在**唯一两个** `Formatter` 构造点——`cli_support.py` 与 `web/app.py`——覆盖引擎 CLI / web / 容器全部输出面，幂等；`session_cache.py` 裸号点修；展示/导出层复用同一实现；含裸号的记录→遮罩的不变量测试） |
| R-T12 | **同一账号被多个执行体重复留痕 ⇒ 报表按行计数偏大**（用户 2026-09-23 报告并定性：**不是业务逻辑问题，是计数口径问题**）。成因：跳过发生在**领取之前**（暂停/取消号不产生 claim、不占租约，设计如此），两个 worker 各走一遍跳过路径、各写一行事件 ⇒ `sign_event_stats` 的 `COUNT(*)` 把同一账号算两次（今日 `user_cancelled` 因此 +1 虚增，生产实测 78 账号/78 行 `session_cache` 泄漏是另一处）。**落点**：`yiban/store/events.py::sign_event_stats`（`GROUP BY day, status` + `COUNT(*)`），唯一消费者 `web/routes/data.py:228` → 日志页统计视图。**修法（minimal）**：`COUNT(*)` → `COUNT(DISTINCT phone)`，使 `cnt` 语义 = **该日该状态的账号数**；建议同时保留一个原始行数字段（如 `row_cnt`）以免丢失"尝试次数"信息。**必须写清的语义边界**：① 各状态桶**分别**按账号去重，故同一天先失败后成功的账号会**同时出现在两个桶**——前端若把各桶相加当"账号总数"仍会偏大，那需要另给"最终状态"口径（按账号取当日最后一条事件）；② 事件**列表**仍会显示两行（那是两个 worker 各自的事实），若要列表也不重复，属写入侧去重（同一 `(phone, day, status, stage)` 只写一次 ⇒ 需唯一索引/迁移或读-写判重），**本批不做、另议**。**排期**：与 R-T11 同轮（都在 T9 前），各自一个提交 | M3（提前至 T9 前） | 已完成（`389650c`；`COUNT(DISTINCT phone)` + `row_cnt`，docstring 两条边界 + "同账号跨两桶各 1"的测试） |
| R-T12b | **前端口径与文案对齐**（R-T12 的必然后续，实施者登记）：`cnt` 变"账号数"后，`web/static/js/pages/data_dashboard.js` 仍按行口径求和与写文案 ⇒ **数字与标签对不上**（`normalizeDaily` 混装两种口径、`renderDist` 把各桶相加当"签到事件总数"、趋势文案写"次/事件"、`renderRateKpi` 口径未声明）。**修法（只改前端）**：分布用 `cnt` 且文案改"账号"；趋势/总数用 `row_cnt` 且文案保持"次/事件"；KPI 二选一口径并在标签写明；`normalizeDaily` 两类聚合分开；新增一条 **node 真跑**的 JS 行为测试（同账号两行 ⇒ 分布 1、事件 2） | M3（提前至 T9 前） | 已完成（`36b21cb`；`normalizeDaily` 拆成 `{accounts, events}` 两桶，分布/日历走账号、趋势/总数走事件、KPI 声明"按事件（尝试）"并在 tooltip 披露偏差方向） |
| R-T14 | **v3 灰度前置清单**（T9 实施者 concerns 中被协调者判定为"开闸前必须确认"的项）：① **失败通知已在本轮补齐**（v2 的最终放弃会发管理员条目/用户失败邮件，v3 原缺 ⇒ 现在对齐；`_alert_slow_sign` 仍按简报推迟，需在 runbook 写明）；② **v3 不写 `sign_claims`** ⇒ 同日若先跑 v2（池里有当日行）再切 v3，`has_undone_accounts_today` 读池会误判补签闸门；纯 v3 日 `stats.total==0` → 回退 `sign-state-*.json`（v3 会写）语义正确 ⇒ **灰度 runbook 必须点名"当日单写者"规则**（与 U6 同源，本项是它的具体症状）；③ `executor_v3.py` 贴线 596/600、`runner.py` 609、`schedule.py` 618（已登记 640）⇒ 下一次改动很可能再触模块规模门禁，T10 前先想好拆分或再放宽；④ `/api/scheduler/executors*` 读的是监督进程写的**文件心跳**，v3 单进程路径没有心跳 ⇒ 页面显示 `idle`（属 T10/灰度批的可观测性缺口）；⑤ `dry_run` 无影子行（有意），影子期只能靠 `plan_stats` 的 `hist`/`peak_per_sec` 与现网落点对账；⑥ **开闸日与 v18 平移 / v20 补账的关系**（F2 修复的残留面，**已由独立复核核定口径**）：机制是真的——`write_plan` 是 `INSERT OR IGNORE` + 主键 `(phone, day)`，当日已有的 `vshard=-1` 历史行会挡住补建；但**`state='pending'` 且 `vshard=-1` 的行不可达**（v18 平移原样复制 `sign_claims.state`，词表只有 `claimed/done/failed`；v20 只写 `done/failed/skipped`；全仓无第二条写 `vshard=-1` 的路径）⇒ **"在配账号领不到"是理论性的**。真实且窄的后果只有一条：**升级当日**已有终态（尤其 `failed`）的账号，在该日的 v3 轮里不会再被重试（一次性、升级窗口内）⇒ 灰度 runbook 只需写明"开闸最好与平移/补账错开日"即可，不必当成硬门；⑦ `planner.py` 头部"通信"段注释略陈旧（仍写"v3 用 `has_plan` 判无计划则降级"），归 M2 注释流；⑧ `attempts.RISK_FAIL_KEYWORDS` 与 `security.WAF_KEYWORDS` 是两份独立维护的同义列表（现仅靠注释对齐）⇒ 建议让前者复用后者（"同一事实两份定义"，M2 一并核） | T10 / 灰度批 | 未开始（① 本轮补） |
| R-T17 | **v3 接管判据的盲区（T10 起跑接管轮的登记项）**：清单 `YIBAN_EXECUTORS` 里配了某执行体，但它**今日从未起跑**（监督进程没拉起 / 起跑前就挂了 / 昨日跑过今日没跑 ⇒ 心跳记录属昨日）时，`state_io.worker_presence` 返回 **`idle`**，而 `_widen_with_dead_peers` 只在 **`stale`** 时接管 ⇒ **其 HRW 分片集内的 `pending` 行整轮无人领取**（计划行由 planner 按 HRW 为全部账号写入，与执行体是否存活无关）。这与刚修的"短轮次永不接管"是**同类静默漏签**，成因不同（"配了没起来" vs "跑一半崩了"）。**处置建议（需你或灰度批拍板）**：① 把"今日无记录"也纳入接管判据——**安全性分析**：误接管（槽位其实稍后才起跑）不会造成重复登录，因为 `claim_batch` 置 `state='claimed'` + 主键 `(phone,day)` 使后来者领不到，`epoch` fencing 保证只有一个结论写回；代价是"晚起的执行体发现自己的分片已被领完、本轮空转"（可接受）；② 或由监督进程对"清单里的槽位本轮没起跑"告警（不改接管语义）。**倾向 ①**（治本，且风险已分析清楚），但属接管语义变更，故不在 T10 内实施 | 灰度批 | 未开始 |
| R-T15 | **T10 复核的登记项**（不修只记）：① **F7** `docs/dev/scheduler-v3.md` 写 `YIBAN_WORKERS` = "并行执行体数（人工优先）"，与代码不符——清单在场时 runner 用 `len(launch_slots())` 派发、**忽略** `args.workers`；② **F8** `schedule.executor_count` 的 docstring 自称"容量公式/预检告警/该开几个执行体三处都调本函数"，实际生产消费者**只有 runner 预检一处**；③ **F11** v3 下保存闸门用 `k=1`、引擎预检用自动 K ⇒ "保存被拒、计划排得下"的分裂在 v3 **换向保留**（闸门偏小；文档 §7 已如实披露）；④ **`_write_sign_state` 不带 fencing**：被回收/fence 掉的尝试仍会把结论写进当日 JSON（与行状态短暂不一致，靠重试收敛）。**另**：F4/F5/F6/F10（预检急切算 K、接管链路零覆盖、源码文本当契约的脆断言、回收不带 `day`）已在 **T10 第二轮修复**中处置 | T10 第二轮 / M2 | 已完成（登记在案；①②③④ 归 M2/灰度批） |
| R-T16 | **M1 期间的 dev 文档未入库**（T10 复核 F9 发现）：`docs/dev/scheduler-v3.md` 与 `tests/test_scheduler_v3_doc.py`、`tests/test_migration_compat.py` 引用的三份文档（`m1-task-rearrangement-20260923.md`、`ledger-dual-version-design-20260923.md`、`upgrade-data-migration-design-20260923.md`）在**本分支与 develop 都不存在** ⇒ 提交物里的交叉引用**悬空**（含**测试**引用，故必须处理）。**裁决（2026-09-24，M1 收尾）：走 (a) 入库**。入库 = **调度线**的 14 份：三份被引用件 + `review-t5-t6`/`review-t7` 两份独立复核 + `must-fix-list` / `doc-writing-contract` / `test-inventory` / `security-slim-plan` + 五份 `reviewfix-10k-*` 设计复核。**不入库三类**：① `docs/dev/handoff-*.md`（两份交接任务书，`.gitignore:161` 的 `HANDOFF*.md` **有意忽略**，保持本地）；② `docs/dev/reviews/**` 与 `review-fix-plan-20260921.md`（属**前端审查线**与 09-19 注释批，与调度线不同源，不搬到本分支）；③ `00-review-stash.diff`（107 KB 工作区 stash 转储，非文档） | M1 收尾 | 已完成（本分支 `docs(dev): 入库调度线设计与验收文档…`） |
| R-T13 | **测试并行化（压缩门禁时长）**：环境已具备（`pytest-xdist 3.8.0` + 24 核；CI 早已声明 `-n auto`）。**实测**：串行 ~400–420s → `pytest -q -n auto` **58–64s（≈6.5×）**，但**默认 `--dist load`（按单条测试分发）确定性失败 1 条**：`tests/test_web_security_gates.py::Batch18FixesTest::test_cookie_path_narrowed_when_base_path_env_set` 报 `401`（日志显示该 app 实例新建空库并跑完 v1~v19，随后管理员登录被拒 ⇒ **同一个类被拆到不同 worker + 进程级 DB 连接单例指向别的库**）；另有 1 条间歇（`tests/test_login_e2e_mock.py::test_full_chain_rehearsal`，3 次挂 1 次）。**采用 `-n auto --dist loadfile`（按文件分发、文件内顺序保留）→ 实测 `2854 passed / 4 skipped / 0 failed / 58.85s`**，与串行数字逐项一致。**待做**：① **门禁命令统一改为** `python -m pytest tests/ -q -n auto --dist loadfile`（本行即为准）；② **CI 的 `.github/workflows/ci.yml` 的 `pytest -q -n auto` 必须加 `--dist loadfile`**——否则那条会确定性失败（现 CI 很可能是红的或靠顺序侥幸绿）；③ 两份交接任务书里的门禁命令同步；④ 那两条顺序依赖用例**登记**（根治是让用例自身隔离：显式重置进程级 DB 单例/env；`loadfile` 是规避而非根治）；⑤ 可选：`pyproject.toml` 的 `addopts` 是否内置该标志（代价是单文件调试也会起多 worker，倾向不加、只在门禁与 CI 显式写） | M1 收尾（T9 后） | **部分完成**：门禁命令（本文件 §5）与 **CI 的 `ci.yml`** 均已改为 `--dist loadfile`（提交 `d5861ab`）；**残留**——本套仍有 **1~2 条对负载/时序敏感的用例**（实测：`test_login_e2e_mock.py::KillYiBanE2ETest::test_full_chain_rehearsal` 3 次挂 1 次、`test_sign_round_guards.py::BatchSignCooldownTest::test_cooldown_blocks_immediate_retry` 2 次挂 1 次；两者单文件并行/串行均全绿 ⇒ 非自包含，属负载/时序或跨文件争用）。**处置建议**：① 短期——门禁遇到失败先单文件串行复跑定性（真失败 vs 间歇），间歇的不算门禁失败；② 根治——把这两条改成不依赖墙钟/负载（假时钟或显式同步），归 **M2 审查流的"测试质量"维度**（它本就要求找"顺序/时序依赖"的用例） |
| R-T8 | **窗口「回退」路径的剩余几何分叉**（全仓清点 `sign_start`/`edge_front_sec`/`edge_config` 消费点后的余项）。已修：`planner.plan_stats` 直方图基点（回退时 06:30 桶键算成 `-30`，影子期对比会错格）、`round._next_retry_at` 重试上界（回退时窗口还开着却判定放不下而放弃重试 = 丢工作）。**仍分叉（登记，未修）**：① `web/services/accounts_data._slot_to_label` 标签基点取原始窗口（回退时"已存偏好"提示与片列表标签不一致）② `_estimate_slot` 回退时 `eff_lo > eff_hi` → 返回空（**fail closed**，不显示错误时段）③ `web/routes/my.py::api_my_time_pref` 的 `window` 字段显示配置原值（与按回退窗口算的 `slots` 并列略不自洽，属"显示配置原值"既定语义）。前提：回退 = **配置病态**（裁剪把窗口吃空；`_schedule_blocks` 已告警并入管理员邮件），故 ①③ 属显示口径、②属 fail-closed | M2 | 未开始 |

---

## 9. M1 收尾记录（2026-09-24）

| 步骤 | 动作 | 结果 |
|------|------|------|
| 1 | **压平中间态**：`dca4030`+`c8d7354`+`c3a0452` 三次提交并作一次（`9bb8380`） | 前两次里 `_has_conclusion` 用集合成员判据，把 12 常量之外的串由「有结论」翻成「无结论」（**较不保守**），第三次才修回谓词 ⇒ 压平后该中间态不再单独存在。做法 `git reset --soft` + `git rebase --onto`，**分支树哈希逐字节相同**（压平前后同为 `4e999a8f7abbdba667e2985e9d39b543e2bed437`）⇒ 内容零变化 |
| 2 | **全量门禁**（WSL） | `ruff check yiban/ tests/ scripts/` clean；`python -m pytest tests/ -q -n auto --dist loadfile` → **2854 passed / 4 skipped / 0 failed / 58.85s** |
| 3 | **文档入库**（R-T16 走 (a)） | 调度线 14 份入本分支；`handoff-*.md`（有意忽略）/ `docs/dev/reviews/**`（前端线）/ `00-review-stash.diff` 三类不入 |
| 4 | **SHA 引用同步** | 重写后 5 份文档共 23 处旧 SHA 按「旧→新」映射改写（`review-t5-t6` 里三合一那处按新事实重述，不再是三个并列 SHA）；文档内 7 位十六进制引用 **0 处无法解析** |
| 5 | **合入 `develop`** | `--no-ff` + 仓库既有的 `merge: …` 消息约定；**不 push**、不触 `main`/`server-web` |
| 6 | **未入 M1 的登记项** | R-T6 / R-T7 / R-T8 / R-T14 / R-T15 / R-T17 均已在案，分别归 M2 与灰度批；S1 排在 M1 合并后、M2 前 |

**压平的可见影响**：本分支 `dca4030` 之后**全部**提交的 SHA 都变了（共 62 个）。文档、简报、复核报告里的旧 SHA 已全部同步；若别处（本地笔记、对话记录）引用过旧 SHA，以 `git log --format='%h %s'` 的**主题行**对齐即可。

**为何值得压平**：这三次提交是同一个收口的连续过程，其中间点语义**方向相反**（同一输入判出相反结论）。develop 收的是逐提交历史，bisect 一旦落在那两次上，会得到"未知状态串不算已有结论"的错误印象——那正是被第三次提交否掉的实现。
